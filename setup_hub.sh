#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# SafePlace Hub — provisioning de una Raspberry Pi desde cero.
#
# Se corre DESDE ADENTRO del repo ya clonado:
#     git clone https://github.com/mackhood/safeplace-hub.git ~/safeplace-hub
#     cd ~/safeplace-hub && bash setup_hub.sh
#
# Es idempotente: se puede volver a correr sin romper nada.
# Paso a paso explicado en docs/provisioning-raspberry.md
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HERE/venv"
SERVICE_NAME="safeplace-hub"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
RUN_USER="${SUDO_USER:-$USER}"

say() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }

say "SafePlace Hub — setup en $HERE (usuario: $RUN_USER)"

# ─── 1. Dependencias del sistema ─────────────────────────────────────
say "[1/6] Dependencias del sistema"
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3 python3-venv python3-pip \
  bluetooth bluez \
  libdbus-1-dev libglib2.0-dev \
  python3-dbus python3-gi python3-pycryptodome python3-requests
# Los cuatro python3-* son para HUB_MODE=xiaomi: ese lector usa el Python del
# sistema (dbus-python + GLib), NO el venv. Instalarlos siempre sale barato y
# evita que el modo xiaomi falle recién al arrancar el servicio.

# ─── 2. Virtualenv (modo garmin: bleak + aiohttp) ───────────────────
say "[2/6] Virtualenv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$HERE/requirements.txt" -q
echo "  bleak   $("$VENV_DIR/bin/pip" show bleak   2>/dev/null | awk '/^Version/{print $2}')"
echo "  aiohttp $("$VENV_DIR/bin/pip" show aiohttp 2>/dev/null | awk '/^Version/{print $2}')"

# ─── 3. Configuración ────────────────────────────────────────────────
say "[3/6] Configuración (.env)"
if [ ! -f "$HERE/.env" ]; then
  cp "$HERE/.env.example" "$HERE/.env"
  sed -i "s|/home/USER|$HOME|g" "$HERE/.env"
  warn ".env creado desde la plantilla — HAY QUE EDITARLO antes de arrancar"
else
  echo "  .env ya existe, no lo toco"
fi
mkdir -p "$HOME/safeplace-gateway"   # destino por defecto de DB_PATH y LOG_FILE_PATH

# ─── 4. Bluetooth ────────────────────────────────────────────────────
say "[4/6] Bluetooth"
# Sin AutoEnable el adaptador queda DOWN después de un reboot y el hub no
# levanta hasta que alguien entra a mano y hace `bluetoothctl power on`.
if ! grep -qE '^\s*AutoEnable\s*=\s*true' /etc/bluetooth/main.conf 2>/dev/null; then
  if grep -q '^\[Policy\]' /etc/bluetooth/main.conf; then
    sudo sed -i '/^\[Policy\]/a AutoEnable=true' /etc/bluetooth/main.conf
  else
    printf '\n[Policy]\nAutoEnable=true\n' | sudo tee -a /etc/bluetooth/main.conf > /dev/null
  fi
  echo "  AutoEnable=true agregado a /etc/bluetooth/main.conf"
else
  echo "  AutoEnable ya estaba activo"
fi
sudo systemctl enable --now bluetooth > /dev/null 2>&1 || true
command -v rfkill > /dev/null && sudo rfkill unblock bluetooth || true

echo "  adaptadores detectados:"
hciconfig 2>/dev/null | awk '/^hci/{name=$1} /UP RUNNING|DOWN/{print "    " name " -> " $1}' || true

# ─── 5. Servicio systemd ─────────────────────────────────────────────
say "[5/6] Servicio systemd ($SERVICE_NAME)"
chmod +x "$HERE/hub_entrypoint.sh"
sed -e "s|/home/safeplace/safeplace-hub|$HERE|g" \
    -e "s|^User=.*|User=$RUN_USER|" \
    "$HERE/systemd/${SERVICE_NAME}.service" | sudo tee "$UNIT_PATH" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" > /dev/null
echo "  unit instalada en $UNIT_PATH"

# ─── 6. Listo ────────────────────────────────────────────────────────
say "[6/6] Setup completo"
cat <<EOF

  Falta configurar antes de arrancar:

    nano $HERE/.env

  Como mínimo:
    HUB_MODE        garmin | xiaomi | both
    BACKEND_URL     URL base del backend (sin path)
    API_KEY         debe coincidir con GATEWAY_API_KEY del backend

    si HUB_MODE=garmin  ->  TARGET_ADDRESSES con la MAC del wearable
    si HUB_MODE=xiaomi  ->  BAND_MAC y BAND_AUTH_KEY
                            (antes hay que emparejar la Band:
                             $VENV_DIR/bin/python tools/pair_band9.py --mac <MAC>)

  Después:
    sudo systemctl start $SERVICE_NAME
    journalctl -u $SERVICE_NAME -f

  Documentación: docs/provisioning-raspberry.md
EOF
