#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# SafePlace BLE Gateway — Setup para Raspberry Pi 5
# Ejecutar con: bash setup_gateway.sh
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

PROJECT_DIR="$HOME/safeplace-gateway"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_NAME="safeplace-gateway"

echo "══════════════════════════════════════════"
echo " SafePlace BLE Gateway — Setup"
echo "══════════════════════════════════════════"

# ─── 1. Dependencias del sistema ─────────────────────────────────────
echo ""
echo "[1/5] Instalando dependencias del sistema..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip bluetooth bluez libdbus-1-dev libglib2.0-dev

# ─── 2. Crear proyecto ──────────────────────────────────────────────
echo ""
echo "[2/5] Creando proyecto en $PROJECT_DIR..."
mkdir -p "$PROJECT_DIR"

# ─── 3. Crear archivos ──────────────────────────────────────────────
echo ""
echo "[3/5] Generando archivos..."

# --- requirements.txt ---
cat > "$PROJECT_DIR/requirements.txt" << 'REQEOF'
bleak>=0.21.0
aiohttp>=3.9.0
REQEOF

# --- .env ---
cat > "$PROJECT_DIR/.env" << 'ENVEOF'
# ═══════════════════════════════════════════════════════════════
# SafePlace BLE Gateway — Configuración
# Editá este archivo con tus valores y reiniciá el servicio:
#   sudo systemctl restart safeplace-gateway
# ═══════════════════════════════════════════════════════════════

# URL del backend en la nube — dejar vacío hasta tener el backend listo
BACKEND_URL=

# Endpoint donde se hace POST con los readings
REPORT_ENDPOINT=/api/heartrate

# Bearer token para autenticación (dejar vacío si no se usa)
API_KEY=

# MACs de los wearables separadas por coma.
# Si está vacío, escanea y conecta a TODOS los dispositivos HR.
# Ejemplo: TARGET_ADDRESSES=A0:28:84:65:BE:F8,B1:33:44:55:66:77
TARGET_ADDRESSES=

# Segundos entre cada guardado en SQLite y reporte al backend
REPORT_INTERVAL=5

# Timeout del scan BLE en segundos
SCAN_TIMEOUT=10

# Segundos antes de reintentar conexión BLE
RECONNECT_DELAY=5

# Tiempo de retención del buffer de reenvío al backend (segundos, default 2 horas)
BUFFER_TTL=7200

# Path de la base SQLite (almacenamiento principal + buffer offline)
DB_PATH=/home/$USER/safeplace-gateway/safeplace.db

# Readings por batch en el flush al backend
FLUSH_BATCH_SIZE=50

# Path del archivo de log
LOG_FILE_PATH=/home/$USER/safeplace-gateway/logger.txt
ENVEOF

# Corregir el path con el usuario real
sed -i "s|\$USER|$USER|g" "$PROJECT_DIR/.env"

# --- ble_gateway.py ---
cat > "$PROJECT_DIR/ble_gateway.py" << 'PYEOF'
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
from pathlib import Path

from bleak import BleakClient, BleakScanner
import aiohttp

# ─── Config ───────────────────────────────────────────────────────────

BACKEND_URL      = os.getenv("BACKEND_URL", "")
REPORT_ENDPOINT  = os.getenv("REPORT_ENDPOINT", "/api/heartrate")
API_KEY          = os.getenv("API_KEY", "")

TARGET_ADDRESSES = [
    a.strip() for a in os.getenv("TARGET_ADDRESSES", "").split(",") if a.strip()
]

SCAN_TIMEOUT     = int(os.getenv("SCAN_TIMEOUT", "10"))
RECONNECT_DELAY  = int(os.getenv("RECONNECT_DELAY", "5"))
REPORT_INTERVAL  = int(os.getenv("REPORT_INTERVAL", "5"))
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
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_hr_sent
            ON heart_rate_log(sent, created_at)
        """)
        self._conn.commit()
        count = self._conn.execute("SELECT COUNT(*) FROM heart_rate_log").fetchone()[0]
        log.info("HeartRateStore listo — %d readings almacenados en total", count)

    def save(self, reading: HRReading) -> int:
        cur = self._conn.execute(
            "INSERT INTO heart_rate_log (bpm, timestamp, device_addr) VALUES (?, ?, ?)",
            (reading.bpm, reading.timestamp, reading.device_address),
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
            "SELECT id, bpm, timestamp, device_addr FROM heart_rate_log WHERE sent=0 ORDER BY id LIMIT ?",
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
                for row_id, bpm, ts, addr in batch:
                    reading = HRReading(bpm=bpm, timestamp=ts, device_address=addr)
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


async def _post_to_backend(session: aiohttp.ClientSession, reading: HRReading) -> bool:
    if not BACKEND_ENABLED:
        return False
    url = f"{BACKEND_URL}{REPORT_ENDPOINT}"
    payload = {"bpm": reading.bpm, "timestamp": reading.timestamp, "deviceAddress": reading.device_address}
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
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

    def on_hr(_sender, data: bytearray):
        nonlocal latest_bpm
        latest_bpm = parse_hr(data)
        log.info("[%s] HR: %d BPM", address, latest_bpm)

    def on_disconnect(_client):
        log.warning("[%s] Dispositivo desconectado", address)
        stop_event.set()

    log.info("Conectando a %s...", address)
    async with BleakClient(address, timeout=15, disconnected_callback=on_disconnect) as client:
        log.info("Conectado a %s", address)
        await client.start_notify(HR_CHAR_UUID, on_hr)

        async with aiohttp.ClientSession() as session:
            while not stop_event.is_set():
                await asyncio.sleep(REPORT_INTERVAL)

                if latest_bpm is not None:
                    reading = HRReading(bpm=latest_bpm, timestamp=time.time(), device_address=address)
                    row_id = store.save(reading)
                    log.debug("[%s] Guardado en SQLite (id=%d)", address, row_id)

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
PYEOF

chmod +x "$PROJECT_DIR/ble_gateway.py"

# ─── 4. Virtualenv + dependencias ────────────────────────────────────
echo ""
echo "[4/5] Creando virtualenv e instalando dependencias..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q
echo "     bleak $("$VENV_DIR/bin/pip" show bleak 2>/dev/null | grep Version | awk '{print $2}')"
echo "     aiohttp $("$VENV_DIR/bin/pip" show aiohttp 2>/dev/null | grep Version | awk '{print $2}')"

# ─── 5. Servicio systemd ────────────────────────────────────────────
echo ""
echo "[5/5] Configurando servicio systemd..."

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << SVCEOF
[Unit]
Description=SafePlace BLE Gateway
After=bluetooth.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$VENV_DIR/bin/python3 $PROJECT_DIR/ble_gateway.py
Restart=always
RestartSec=10

# Permisos BLE
AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN
NoNewPrivileges=true

# Logs
StandardOutput=journal
StandardError=journal
SyslogIdentifier=safeplace-gw

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}

# ─── Listo ───────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo " Setup completado!"
echo "══════════════════════════════════════════"
echo ""
echo " Proyecto:  $PROJECT_DIR"
echo " SQLite:    $PROJECT_DIR/safeplace.db"
echo " Log:       $PROJECT_DIR/logger.txt"
echo " Config:    $PROJECT_DIR/.env"
echo ""
echo " Comandos útiles:"
echo "   nano $PROJECT_DIR/.env                   # editar config"
echo "   sudo systemctl start $SERVICE_NAME       # arrancar"
echo "   sudo systemctl stop $SERVICE_NAME        # parar"
echo "   sudo systemctl restart $SERVICE_NAME     # reiniciar"
echo "   sudo journalctl -u $SERVICE_NAME -f      # ver logs en vivo"
echo "   tail -f $PROJECT_DIR/logger.txt          # ver log en tiempo real"
echo "   sqlite3 $PROJECT_DIR/safeplace.db 'SELECT * FROM heart_rate_log ORDER BY id DESC LIMIT 20;'"
echo ""
echo " Test manual (sin systemd):"
echo "   cd $PROJECT_DIR && source venv/bin/activate"
echo "   python3 ble_gateway.py"
echo ""
echo " Cuando tengas el backend listo, editá .env y poné:"
echo "   BACKEND_URL=https://tu-backend.com"
echo "   sudo systemctl restart $SERVICE_NAME"
echo "══════════════════════════════════════════"
