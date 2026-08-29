# SafePlace — Hub (Gateway BLE)

Corre en la Raspberry Pi. Toma la frecuencia cardíaca de los wearables Garmin
por **BLE (Heart Rate Service)** y la reenvía por API REST al backend de
SafePlace en la nube.

## Ejecución

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

## Herramientas de prueba (`tools/`)

- `inject_measurements.py` — postea mediciones sintéticas al backend sin BLE.
  Útil para CP-E2E-* (ver `docs/e2e/CP-E2E-04.md` del repo del backend).

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```
