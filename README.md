# SafePlace — Hub (Gateway BLE)

Corre en la Raspberry Pi. Toma la frecuencia cardíaca de un wearable por BLE y
la reenvía por API REST al backend de SafePlace en la nube.

Soporta dos familias de wearables, y **cada Raspberry lee una u otra según la
variable `HUB_MODE` del `.env`**:

| `HUB_MODE` | Lector | Wearables |
|-----------|--------|-----------|
| `garmin` (default) | `ble_gateway.py` | Los que exponen el Heart Rate Service estándar (`0x180D`): Garmin, iGPSPORT… |
| `xiaomi` | `tools/xiaomi_band9.py` | Xiaomi Smart Band 9 Active (protocolo propietario MiWear SPP v2) |
| `both` | los dos | — |

`hub_entrypoint.sh` es el `ExecStart` del servicio y elige el lector. Cada modo
usa un intérprete distinto: el Garmin corre en el venv (bleak + aiohttp) y la
Xiaomi con el Python del sistema (`python3-dbus`, `python3-gi`).

## Puesta en marcha

Para levantar una Raspberry desde cero, seguí
**[docs/provisioning-raspberry.md](docs/provisioning-raspberry.md)**. Resumido:

```bash
git clone https://github.com/mackhood/safeplace-hub.git ~/safeplace-hub
cd ~/safeplace-hub
bash setup_hub.sh          # deps, venv, bluetooth, systemd
nano .env                  # HUB_MODE, BACKEND_URL, API_KEY, MACs
sudo systemctl start safeplace-hub
journalctl -u safeplace-hub -f
```

> Ojo: dar de alta la MAC del wearable en el backend y asignarla a un operario
> **no es opcional**. Si falta, el hub lee pulso pero descarta cada medición.
> Ver §7 de la guía de provisioning.

## Ejecución manual (desarrollo)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # editar BACKEND_URL, API_KEY, TARGET_ADDRESSES
python ble_gateway.py
```

Guarda todas las lecturas en SQLite (`heart_rate_log`); las que no se
pudieron enviar quedan en cola (`sent=0`) y se reintentan.

## Nivel de actividad (proxy derivado de FC — ADR-14)

El Garmin en modo broadcast de FC no expone actividad. El gateway la **estima**
a partir de la variación de la pulsación y la manda como `nivelActividad`
(0.0–1.0) en cada medición. Alimenta la detección de sobreesfuerzo (H0011) y
el monitoreo — **no** es el disparador de la alerta de inactividad prolongada.

| Var | Default | Qué hace |
|---|---|---|
| `ACTIVITY_MODE` | `hr-proxy` | `hr-proxy` / `fixed` / `off` |
| `ACTIVITY_FIXED_VALUE` | `0.0` | valor en modo `fixed` |
| `ACTIVITY_PROXY_WINDOW` | `6` | nº de lecturas de la ventana |
| `ACTIVITY_MOVEMENT_FLOOR` | `0.08` | por debajo → se cuantiza a 0.0 |
| `RESTING_HR` | `65` | FC de reposo de referencia |

## Detección de "wearable congelado" (CP-E2E-04)

Si llegan `STUCK_READINGS_THRESHOLD` (default 12) lecturas seguidas con
**exactamente** la misma pulsación, el wearable no está midiendo de verdad
(fuera de la muñeca): el gateway reporta `DESCONECTADO` y deja de enviar
mediciones hasta que llega un valor genuinamente distinto (entonces reporta
`CONECTADO`).

## Xiaomi Smart Band 9 Active

La Band **no** expone el Heart Rate Service estándar: el pulso sólo sale por
MiWear SPP v2, que exige abrir una sesión y autenticarse antes de responder
nada. El protocolo está documentado byte por byte en
[docs/xiaomi-band9-active-protocol.md](docs/xiaomi-band9-active-protocol.md),
incluidos los callejones sin salida, para no volver a recorrerlos.

Dos requisitos que no son obvios:

- **Bonding**, una vez por Raspberry (`tools/pair_band9.py`). Sin él, toda
  operación GATT falla con `Unlikely Error (0x0E)`.
- **`BAND_AUTH_KEY`**, un secreto de 16 bytes propio de cada dispositivo. Va en
  el `.env`, nunca en el repo: con esa clave y estando en alcance BLE cualquiera
  puede leer el pulso de quien lleve la Band.

## Herramientas (`tools/`)

- `xiaomi_band9.py` — lector de la Band 9 Active. `--once`, `--poll N`, `--feed`.
- `pair_band9.py` — bonding de la Band, se corre una vez por Raspberry.
- `inject_measurements.py` — postea mediciones sintéticas al backend sin BLE.
  Útil para CP-E2E-* (ver `docs/e2e/CP-E2E-04.md` del repo del backend).

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```
