#!/usr/bin/env python3
"""
SafePlace — BLE Gateway (Raspberry Pi 5)
Escanea dispositivos BLE con servicio Heart Rate, se conecta,
guarda en SQLite + logger.txt, y opcionalmente reporta al backend.
"""

from __future__ import annotations

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
from hr_store import HRReading, HeartRateStore, BackendFlusher

# ─── .env ─────────────────────────────────────────────────────────────
# `python ble_gateway.py` a mano tiene que ver la misma config que el
# servicio systemd (que la inyecta con EnvironmentFile). Cargamos el .env
# que esté al lado del script; no pisa nada que ya venga del entorno.
def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv(Path(__file__).with_name(".env"))

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

# Modo de prueba: fuerza el id de dispositivo del backend en vez de resolverlo
# por MAC. Necesario cuando el "wearable" es un simulador (macOS/Windows), cuya
# dirección BLE es del hardware/aleatoria y no se puede registrar como MAC fija.
FORCE_DEVICE_ID  = os.getenv("FORCE_DEVICE_ID", "").strip()
FORCE_DEVICE_ID  = int(FORCE_DEVICE_ID) if FORCE_DEVICE_ID.isdigit() else None

# Filtro de nombre para el auto-scan: si está seteado, el hub solo se conecta a
# dispositivos cuyo nombre BLE contenga este texto (case-insensitive). Evita
# engancharse a un wearable ajeno (otro reloj / banda) que también exponga el
# Heart Rate Service. Ej: SCAN_NAME_FILTER=SafePlace-Sim
SCAN_NAME_FILTER = os.getenv("SCAN_NAME_FILTER", "").strip().lower()

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

# El log a archivo es best-effort: si el directorio no existe se intenta crear,
# y si aun así falla (permisos, ejecución en un test) el gateway sigue
# funcionando solo con el log a consola/journald.
try:
    Path(LOG_FILE_PATH).parent.mkdir(parents=True, exist_ok=True)
    _fh = logging.FileHandler(LOG_FILE_PATH)
    _fh.setFormatter(_fmt)
    logging.getLogger().addHandler(_fh)
except OSError as _e:
    log.warning("No se pudo abrir el log de archivo %s: %s", LOG_FILE_PATH, _e)

# HRReading / HeartRateStore / BackendFlusher viven en hr_store.py (sin
# dependencias de BLE ni red, para poder testear la resiliencia — CP-E2E-06).


# ─── Funciones core ──────────────────────────────────────────────────

def parse_hr(data: bytearray) -> int:
    flags = data[0]
    if flags & 0x01:
        return int.from_bytes(data[1:3], "little")
    return data[1]


# MAC BLE -> id numérico de dispositivo en el backend (H0007). El backend
# identifica todo por id, no por MAC; se resuelve una vez por dirección
# (con reintento si todavía no está registrada) y se cachea en memoria.
_device_id_cache: dict = {}


async def resolve_device_id(session: aiohttp.ClientSession, address: str):
    if not BACKEND_ENABLED:
        return None
    if FORCE_DEVICE_ID is not None:
        return FORCE_DEVICE_ID
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
                log.info("[%s] Backend OK: %d BPM (HTTP %d)", reading.device_address, reading.bpm, resp.status)
                return True
            # 409 DUPLICADO = la medición ya está persistida (reintento del
            # backlog, o reinicio del proceso entre el envío y el mark_sent).
            # Se trata como éxito para que salga de la cola y no se reintente.
            if resp.status == 409:
                log.debug("[%s] Backend 409 (duplicado, ya persistida) — se marca enviada", reading.device_address)
                return True
            body = await resp.text()
            log.warning("Backend HTTP %d: %s", resp.status, body[:200])
            return False
    except Exception as e:
        log.error("Error enviando al backend: %s", e)
        return False


def _es_nuestro_hr(device, adv) -> bool:
    uuids = [str(u).lower() for u in adv.service_uuids]
    if HR_SERVICE_UUID not in uuids:
        return False
    if SCAN_NAME_FILTER:
        name = (device.name or adv.local_name or "").lower()
        if SCAN_NAME_FILTER not in name:
            return False
    return True


async def _ubicar_wearable(want_address, timeout: int):
    """Devuelve un BLEDevice listo para conectar.

    Se usa BleakScanner.find_device_by_* (no discover()+connect a mano): bleak
    maneja internamente el timing de frenar el discovery antes del Connect(),
    que en BlueZ es delicado — con el scanner activo, Connect() se cancela
    (`br-connection-canceled`); si se frena demasiado antes, el device se
    descarta (`device 'dev_XX' not found`).
    """
    if want_address:
        device = await BleakScanner.find_device_by_address(want_address, timeout=timeout)
    else:
        device = await BleakScanner.find_device_by_filter(
            lambda d, adv: _es_nuestro_hr(d, adv), timeout=timeout
        )
    if device is None:
        raise RuntimeError("el wearable no apareció en el scan")
    return device


async def monitor_device(target, stop_event: asyncio.Event, store: HeartRateStore, flusher: BackendFlusher):
    # `target` puede ser una dirección (str, modo TARGET_ADDRESSES) o None
    # (auto-scan). Si viene un BLEDevice, sólo se usa su dirección como pista;
    # el device se relocaliza fresco en cada intento.
    want_address = target if isinstance(target, str) else None
    latest_bpm: int | None = None
    estimator = estimator_from_env(os.environ)

    # CP-E2E-04: si el wearable repite exactamente la misma pulsación
    # STUCK_READINGS_THRESHOLD veces seguidas, no está midiendo de verdad
    # (fuera de la muñeca) — se reporta DESCONECTADO y se dejan de enviar
    # mediciones hasta que llegue un valor genuinamente distinto.
    stuck_run = {"bpm": None, "count": 0, "reported": False}

    log.info("Buscando wearable%s...", f" {want_address}" if want_address else " (auto-scan)")
    device = await _ubicar_wearable(want_address, SCAN_TIMEOUT)
    address = device.address

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
        async with BleakClient(device, timeout=15, disconnected_callback=on_disconnect) as client:
            log.info("Conectado a %s", address)
            # Suscribirse a las notificaciones ANTES de avisar al backend: el
            # POST a Render puede tardar en frío y el wearable corta el link si
            # nadie se suscribe en los primeros segundos.
            await client.start_notify(HR_CHAR_UUID, on_hr)
            await report_connection_state(session, device_id, "CONECTADO")

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


async def monitor_loop(target, store: HeartRateStore, flusher: BackendFlusher, stop_event: asyncio.Event):
    address = getattr(target, "address", target) or "auto-scan"
    while not stop_event.is_set():
        device_stop = asyncio.Event()
        try:
            await monitor_device(target, device_stop, store, flusher)
        except Exception as e:
            log.error("[%s] Error en monitor: %s", address, e)

        if stop_event.is_set():
            break

        log.info("[%s] Reconectando en %ds...", address, RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)


async def run():
    stop_event = asyncio.Event()
    store = HeartRateStore(DB_PATH)
    # Al arrancar, dar por perdido lo que quedó en el buffer y ya está más
    # viejo que BUFFER_TTL: reenviarlo solo genera 409 DUPLICADO auditados.
    store.purge_stale(BUFFER_TTL)
    flusher = BackendFlusher(
        store, _post_to_backend, enabled=BACKEND_ENABLED,
        batch_size=FLUSH_BATCH_SIZE, ttl_seconds=BUFFER_TTL,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    # Modo TARGET_ADDRESSES: un monitor por dirección fija. Auto-scan: un solo
    # monitor que localiza el wearable por HR Service + SCAN_NAME_FILTER en cada
    # intento (el scan+conexión viven dentro de monitor_device para que BlueZ no
    # descarte el device entre discover() y Connect()).
    targets = list(TARGET_ADDRESSES) if TARGET_ADDRESSES else [None]

    log.info(
        "Lanzando %d monitor(es): %s",
        len(targets),
        [t or "auto-scan" for t in targets],
    )

    tasks = [
        asyncio.create_task(monitor_loop(t, store, flusher, stop_event))
        for t in targets
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
