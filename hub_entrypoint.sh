#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# SafePlace Hub — punto de entrada del servicio systemd.
#
# Elige qué lector de wearables levantar según HUB_MODE del .env:
#
#   HUB_MODE=garmin   (default)  ble_gateway.py — wearables con servicio HR
#                                estándar (0x180D): Garmin, iGPSPORT, etc.
#   HUB_MODE=xiaomi              tools/xiaomi_band9.py — Xiaomi Smart Band 9
#                                Active, que NO expone 0x180D y habla MiWear
#                                SPP v2 (ver docs/xiaomi-band9-active-protocol.md)
#   HUB_MODE=both                los dos a la vez
#
# Cada modo usa un intérprete distinto y eso no es accidental:
#   - ble_gateway.py      -> venv, necesita bleak + aiohttp
#   - tools/xiaomi_band9.py -> python del sistema, necesita python3-dbus y
#                              python3-gi, que no viven en el venv
# ═══════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$HERE/venv/bin/python"
SYS_PY="${SYSTEM_PYTHON:-/usr/bin/python3}"

HUB_MODE="${HUB_MODE:-garmin}"
BAND_POLL_INTERVAL="${BAND_POLL_INTERVAL:-5}"

start_garmin() {
  [ -x "$VENV_PY" ] || { echo "falta el venv en $VENV_PY — corré setup_hub.sh" >&2; exit 1; }
  echo "[hub] modo garmin — $VENV_PY ble_gateway.py"
  exec "$VENV_PY" "$HERE/ble_gateway.py"
}

start_xiaomi() {
  echo "[hub] modo xiaomi — $SYS_PY tools/xiaomi_band9.py"
  exec "$SYS_PY" "$HERE/tools/xiaomi_band9.py" --poll "$BAND_POLL_INTERVAL" --feed
}

case "$HUB_MODE" in
  garmin) start_garmin ;;
  xiaomi) start_xiaomi ;;
  both)
    # Un hijo que muere no debe dejar al otro huérfano: si cae cualquiera,
    # salimos y systemd reinicia el conjunto.
    "$SYS_PY" "$HERE/tools/xiaomi_band9.py" --poll "$BAND_POLL_INTERVAL" --feed &
    XIAOMI_PID=$!
    "$VENV_PY" "$HERE/ble_gateway.py" &
    GARMIN_PID=$!
    trap 'kill "$XIAOMI_PID" "$GARMIN_PID" 2>/dev/null' TERM INT
    wait -n "$XIAOMI_PID" "$GARMIN_PID"
    kill "$XIAOMI_PID" "$GARMIN_PID" 2>/dev/null
    wait
    exit 1
    ;;
  *)
    echo "HUB_MODE inválido: '$HUB_MODE' (esperaba garmin | xiaomi | both)" >&2
    exit 1
    ;;
esac
