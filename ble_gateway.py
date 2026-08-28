#!/usr/bin/env python3
"""
SafePlace — BLE Gateway (Raspberry Pi 5)
Escanea dispositivos BLE con servicio Heart Rate, se conecta,
guarda en SQLite + logger.txt, y opcionalmente reporta al backend.
"""

import asyncio
import logging
import sqlite3
import time
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner
import aiohttp

from activity import estimator_from_env

# ─── Config ───────────────────────────────────────────────────────────

# BACKEND_URL es la base del backend real de SafePlace (ej. http://host:8000,
# sin path). Los paths de la API (/api/v1/mediciones, /api/v1/dispositivos/...)
# son fijos, no configurables — son el contrato real del backend.
BACKEND_URL      = os.getenv("BACKEND_URL", "")
# Debe coincidir con GATEWAY_API_KEY del backend (header x-device-api-key,
# mismo middleware que usa el resto del sistema para autenticar al gateway).
API_KEY          = os.getenv("API_KEY", "")

TARGET_ADDRESSES = [
    a.strip() for a in os.getenv("TARGET_ADDRESSES", "").split(",") if a.strip()
]

SCAN_TIMEOUT     = int(os.getenv("SCAN_TIMEOUT", "10"))
RECONNECT_DELAY  = int(os.getenv("RECONNECT_DELAY", "5"))
REPORT_INTERVAL  = int(os.getenv("REPORT_INTERVAL", "5"))

# CP-E2E-04: N lecturas seguidas con EXACTAMENTE la misma pulsación => el
# wearable no está midiendo de verdad (fuera de la muñeca) y se reporta
# DESCONECTADO. A REPORT_INTERVAL=5s, 12 ≈ 1 min de pulso congelado.
STUCK_READINGS_THRESHOLD = int(os.getenv("STUCK_READINGS_THRESHOLD", "12"))
BUFFER_TTL       = int(os.getenv("BUFFER_TTL", "7200"))
FLUSH_BATCH_SIZE = int(os.getenv("FLUSH_BATCH_SIZE", "50"))

DB_PATH      = os.getenv("DB_PATH", str(Path.home() / "safeplace-gateway" / "safeplace.db"))
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", str(Path.home() / "safeplace-gateway" / "logger.txt"))

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_CHAR_UUID    = "00002a37-0000-1000-8000-00805f9b34fb"

BACKEND_ENABLED = bool(BACKEND_URL)

# ─── Logging ──────────────────────────────────────────────────────────

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("ble-gateway")

_fh = logging.FileHandler(LOG_FILE_PATH)
_fh.setFormatter(_fmt)
logging.getLogger().addHandler(_fh)

# ─── Modelo ───────────────────────────────────────────────────────────

@dataclass
class HRReading:
    bpm: int
    timestamp: float
    device_address: str
    actividad: float | None = None


# ─── Almacenamiento principal (heart_rate_log) ────────────────────────

class HeartRateStore:
    """
    Almacenamiento primario de todos los readings en SQLite.
    Los datos nunca se borran automáticamente.
    sent=0 → pendiente de enviar al backend; sent=1 → ya enviado.
    """

    def __init__(self, db_path: str = DB_PATH):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS heart_rate_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                bpm         INTEGER NOT NULL,
                timestamp   REAL    NOT NULL,
                device_addr TEXT    NOT NULL,
                sent        INTEGER NOT NULL DEFAULT 0,
                created_at  REAL    NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        # Migración in-place para DBs anteriores (SQLite no tiene ADD COLUMN
        # IF NOT EXISTS): nivel de actividad estimado por el gateway.
        try:
            self._conn.execute("ALTER TABLE heart_rate_log ADD COLUMN actividad REAL")
        except sqlite3.OperationalError:
            pass  # la columna ya existe
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_hr_sent
            ON heart_rate_log(sent, created_at)
        """)
        self._conn.commit()
        count = self._conn.execute("SELECT COUNT(*) FROM heart_rate_log").fetchone()[0]
        log.info("HeartRateStore listo — %d readings almacenados en total", count)

    def save(self, reading: HRReading) -> int:
        cur = self._conn.execute(
            "INSERT INTO heart_rate_log (bpm, timestamp, device_addr, actividad) VALUES (?, ?, ?, ?)",
            (reading.bpm, reading.timestamp, reading.device_address, reading.actividad),
        )
        self._conn.commit()
        return cur.lastrowid

    def mark_sent(self, ids: list):
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(
            f"UPDATE heart_rate_log SET sent=1 WHERE id IN ({placeholders})", ids
        )
        self._conn.commit()

    def fetch_unsent(self, limit: int = FLUSH_BATCH_SIZE) -> list:
        return self._conn.execute(
            "SELECT id, bpm, timestamp, device_addr, actividad FROM heart_rate_log WHERE sent=0 ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def close(self):
        self._conn.close()


# ─── Buffer de reenvío al backend ────────────────────────────────────

class BackendFlusher:
    """
    Usa la tabla heart_rate_log (sent=0) como cola de reenvío.
    Solo activo cuando BACKEND_ENABLED=True.
    """

    def __init__(self, store: HeartRateStore):
        self._store = store
        self._lock = asyncio.Lock()

    async def flush(self, session: aiohttp.ClientSession) -> int:
        if not BACKEND_ENABLED or self._lock.locked():
            return 0

        async with self._lock:
            total_sent = 0
            while True:
                batch = self._store.fetch_unsent()
                if not batch:
                    break

                sent_ids = []
                for row_id, bpm, ts, addr, actividad in batch:
                    reading = HRReading(bpm=bpm, timestamp=ts, device_address=addr, actividad=actividad)
                    ok = await _post_to_backend(session, reading)
                    if ok:
                        sent_ids.append(row_id)
                    else:
                        break

                self._store.mark_sent(sent_ids)
                total_sent += len(sent_ids)

                if len(sent_ids) < len(batch):
                    break

            if total_sent > 0:
                log.info("Backend flush: %d readings enviados", total_sent)
            return total_sent


# ─── Funciones core ──────────────────────────────────────────────────

def parse_hr(data: bytearray) -> int:
    flags = data[0]
    if flags & 0x01:
        return int.from_bytes(data[1:3], "little")
    return data[1]


async def scan_for_hr_devices() -> list:
    log.info("Escaneando dispositivos BLE (timeout=%ds)...", SCAN_TIMEOUT)
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT, return_adv=True)
    found = []

    for address, (device, adv) in devices.items():
        uuids = [str(u).lower() for u in adv.service_uuids]
        name = device.name or "sin nombre"
        log.debug("  %s | %s | UUIDs: %s", address, name, uuids)
        if HR_SERVICE_UUID in uuids:
            log.info("Dispositivo HR encontrado: %s (%s)", address, name)
            found.append(address)

    if not found:
        log.warning("No se encontró ningún dispositivo con servicio HR")
    else:
        log.info("Total dispositivos HR: %d", len(found))
    return found


# MAC BLE -> id numérico de dispositivo en el backend (H0007). El backend
# identifica todo por id, no por MAC; se resuelve una vez por dirección
# (con reintento si todavía no está registrada) y se cachea en memoria.
_device_id_cache: dict = {}


async def resolve_device_id(session: aiohttp.ClientSession, address: str):
    if not BACKEND_ENABLED:
        return None
    if address in _device_id_cache:
        return _device_id_cache[address]

    url = f"{BACKEND_URL}/api/v1/dispositivos/lookup"
    headers = {"x-device-api-key": API_KEY} if API_KEY else {}
    try:
        async with session.get(
            url, params={"mac": address}, headers=headers, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                device_id = (data.get("data") or {}).get("id")
                if device_id is not None:
                    _device_id_cache[address] = device_id
                    log.info("[%s] Resuelto a dispositivo id=%s", address, device_id)
                return device_id
            log.warning(
                "[%s] No se pudo resolver el dispositivo (HTTP %d) — "
                "¿falta cargar esta MAC en el backend?", address, resp.status,
            )
            return None
    except Exception as e:
        log.error("[%s] Error resolviendo dispositivo: %s", address, e)
        return None


async def report_connection_state(session: aiohttp.ClientSession, device_id, estado: str):
    if not BACKEND_ENABLED or device_id is None:
        return
    url = f"{BACKEND_URL}/api/v1/dispositivos/{device_id}/estado-conexion"
    headers = {"x-device-api-key": API_KEY} if API_KEY else {}
    try:
        async with session.post(
            url, json={"estado": estado}, headers=headers, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 300:
                body = await resp.text()
                log.warning("Reporte de estado %s: HTTP %d %s", estado, resp.status, body[:200])
    except Exception as e:
        log.error("Error reportando estado %s: %s", estado, e)


async def _post_to_backend(session: aiohttp.ClientSession, reading: HRReading) -> bool:
    if not BACKEND_ENABLED:
        return False

    device_id = await resolve_device_id(session, reading.device_address)
    if device_id is None:
        log.warning("[%s] Sin dispositivo asociado en el backend — medición no enviada", reading.device_address)
        return False

    url = f"{BACKEND_URL}/api/v1/mediciones"
    timestamp_iso = datetime.fromtimestamp(reading.timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    payload = {
        "idDispositivo": device_id,
        "timestamp": timestamp_iso,
        "frecuenciaCardiaca": reading.bpm,
    }
    if reading.actividad is not None:
        payload["nivelActividad"] = reading.actividad
    headers = {"x-device-api-key": API_KEY} if API_KEY else {}
    try:
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status < 300:
                log.debug("Backend OK: %d BPM → HTTP %d", reading.bpm, resp.status)
                return True
            body = await resp.text()
            log.warning("Backend HTTP %d: %s", resp.status, body[:200])
            return False
    except Exception as e:
        log.error("Error enviando al backend: %s", e)
        return False


async def monitor_device(address: str, stop_event: asyncio.Event, store: HeartRateStore, flusher: BackendFlusher):
    latest_bpm: int | None = None
    estimator = estimator_from_env(os.environ)

    # CP-E2E-04: si el wearable repite exactamente la misma pulsación
    # STUCK_READINGS_THRESHOLD veces seguidas, no está midiendo de verdad
    # (fuera de la muñeca) — se reporta DESCONECTADO y se dejan de enviar
    # mediciones hasta que llegue un valor genuinamente distinto.
    stuck_run = {"bpm": None, "count": 0, "reported": False}

    def on_hr(_sender, data: bytearray):
        nonlocal latest_bpm
        latest_bpm = parse_hr(data)
        log.info("[%s] HR: %d BPM", address, latest_bpm)

    async with aiohttp.ClientSession() as session:
        device_id = await resolve_device_id(session, address)

        def on_disconnect(_client):
            log.warning("[%s] Dispositivo desconectado", address)
            stop_event.set()
            asyncio.create_task(report_connection_state(session, device_id, "DESCONECTADO"))

        log.info("Conectando a %s...", address)
        async with BleakClient(address, timeout=15, disconnected_callback=on_disconnect) as client:
            log.info("Conectado a %s", address)
            await report_connection_state(session, device_id, "CONECTADO")
            await client.start_notify(HR_CHAR_UUID, on_hr)

            while not stop_event.is_set():
                await asyncio.sleep(REPORT_INTERVAL)

                if latest_bpm is None:
                    continue

                # --- Detección de pulso "congelado" ---
                if latest_bpm == stuck_run["bpm"]:
                    stuck_run["count"] += 1
                else:
                    if stuck_run["reported"]:
                        log.info("[%s] Pulso vuelve a variar (%d BPM) — CONECTADO", address, latest_bpm)
                        await report_connection_state(session, device_id, "CONECTADO")
                    stuck_run.update(bpm=latest_bpm, count=1, reported=False)

                if stuck_run["count"] >= STUCK_READINGS_THRESHOLD:
                    if not stuck_run["reported"]:
                        log.warning(
                            "[%s] %d lecturas iguales seguidas (%d BPM) — se reporta DESCONECTADO",
                            address, stuck_run["count"], latest_bpm,
                        )
                        await report_connection_state(session, device_id, "DESCONECTADO")
                        stuck_run["reported"] = True
                    continue  # no se guarda ni se envía un pulso congelado

                actividad = estimator.update(latest_bpm)
                reading = HRReading(
                    bpm=latest_bpm, timestamp=time.time(),
                    device_address=address, actividad=actividad,
                )
                row_id = store.save(reading)
                log.debug("[%s] Guardado en SQLite (id=%d, act=%s)", address, row_id, actividad)

                if BACKEND_ENABLED:
                    ok = await _post_to_backend(session, reading)
                    if ok:
                        store.mark_sent([row_id])
                        await flusher.flush(session)
                    else:
                        log.warning("[%s] Backend no disponible — reading en cola (id=%d)", address, row_id)


async def monitor_loop(address: str, store: HeartRateStore, flusher: BackendFlusher, stop_event: asyncio.Event):
    while not stop_event.is_set():
        device_stop = asyncio.Event()
        try:
            await monitor_device(address, device_stop, store, flusher)
        except Exception as e:
            log.error("[%s] Error en monitor: %s", address, e)

        if stop_event.is_set():
            break

        log.info("[%s] Reconectando en %ds...", address, RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)


async def run():
    stop_event = asyncio.Event()
    store = HeartRateStore()
    flusher = BackendFlusher(store)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    addresses = list(TARGET_ADDRESSES)
    if not addresses:
        while not stop_event.is_set():
            addresses = await scan_for_hr_devices()
            if addresses:
                break
            log.info("Reintentando scan en %ds...", RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

    if stop_event.is_set():
        store.close()
        return

    log.info("Lanzando monitor para %d dispositivo(s): %s", len(addresses), addresses)

    tasks = [
        asyncio.create_task(monitor_loop(addr, store, flusher, stop_event))
        for addr in addresses
    ]

    await stop_event.wait()

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    store.close()
    log.info("Gateway detenido")


def main():
    log.info("=== SafePlace BLE Gateway ===")
    log.info("Backend: %s", BACKEND_URL if BACKEND_ENABLED else "DESACTIVADO (se activa poniendo BACKEND_URL en .env)")
    log.info("SQLite:  %s", DB_PATH)
    log.info("Log:     %s", LOG_FILE_PATH)
    log.info("Target:  %s", ", ".join(TARGET_ADDRESSES) if TARGET_ADDRESSES else "(auto-scan all HR devices)")
    asyncio.run(run())


if __name__ == "__main__":
    main()
