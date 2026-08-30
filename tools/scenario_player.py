#!/usr/bin/env python3
"""
SafePlace — Reproductor de escenarios de prueba E2E (sin BLE, sin tocar la DB).

Postea mediciones sintéticas al backend real por el MISMO endpoint que usa el
gateway (`POST /api/v1/mediciones`, header `x-device-api-key`). Sirve para
CP-E2E-02 (fatiga), CP-E2E-03 (sobreesfuerzo), CP-E2E-01/07/08 y variantes de
paquete inválido, que son difíciles o lentos de reproducir con un wearable
físico.

Respeta las restricciones del plan de testing:
  - NO inserta en la base de datos.
  - NO genera alertas manualmente — las dispara el procesamiento normal.
  - El backend recibe los datos por su flujo real.

Precondiciones de estado (operario activo, wearable asociado, consentimiento,
umbrales) se preparan aparte (UI de admin / SQL). Este script solo emite.

Ejemplos:

  # CP-E2E-01 — medición normal, sin alerta
  python tools/scenario_player.py --scenario normal --device-id 8 \
      --backend-url "$BACKEND_URL" --api-key "$API_KEY"

  # CP-E2E-02 — fatiga (ventana sostenida de FC alta, retrodatada)
  python tools/scenario_player.py --scenario fatiga --device-id 8 \
      --fc 150 --window-minutes 12 --backend-url "$BACKEND_URL" --api-key "$API_KEY"

  # CP-E2E-03 — sobreesfuerzo (FC alta + actividad alta, puntual)
  python tools/scenario_player.py --scenario sobreesfuerzo --device-id 8 \
      --fc 185 --actividad 0.9 --backend-url "$BACKEND_URL" --api-key "$API_KEY"

  # CP-E2E-07 — wearable no asociado  (device-id sin asignación vigente)
  python tools/scenario_player.py --scenario no-asociado --device-id 99 ...

  # CP-E2E-08 — consentimiento no otorgado/revocado  (device-id de un
  #             operario sin consentimiento vigente)
  python tools/scenario_player.py --scenario sin-consentimiento --device-id 2 ...

  # paquetes inválidos (RF-04 / H0008)
  python tools/scenario_player.py --scenario invalida --device-id 8 ...
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


def iso(ts):
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def post(url, api_key, payload, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-device-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode()[:400]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def _send(url, key, payload, label="", raw=None):
    status, text = post(url, key, payload, raw=raw)
    ok = 200 <= status < 300
    mark = "  ok " if ok else "FALLO"
    print(f"  [{mark}] HTTP {status:<3} {label}  {'' if ok else text}")
    return status, text


# ─── Escenarios ──────────────────────────────────────────────────────

def escenario_normal(url, key, args):
    print("CP-E2E-01 — medición normal (esperado: 201, sin alerta)")
    p = {"idDispositivo": args.device_id, "timestamp": iso(datetime.now(timezone.utc)),
         "frecuenciaCardiaca": args.fc or 74, "nivelActividad": args.actividad if args.actividad is not None else 0.2}
    _send(url, key, p, "medición dentro de rango")
    print("→ verificá en Supervisor→Monitoreo la última medición y que NO haya alerta nueva.")


def escenario_fatiga(url, key, args):
    fc = args.fc or 150
    win = args.window_minutes
    print(f"CP-E2E-02 — fatiga: FC={fc} sostenida {win} min (esperado: alerta FATIGA / Media)")
    # Ventana retrodatada: la más antigua a win+2 min, una por minuto, todas >= umbral.
    n = win + 2
    base = datetime.now(timezone.utc) - timedelta(minutes=n)
    for i in range(n + 1):
        ts = base + timedelta(minutes=i)
        p = {"idDispositivo": args.device_id, "timestamp": iso(ts),
             "frecuenciaCardiaca": fc + (i % 3), "nivelActividad": 0.3}
        _send(url, key, p, f"t-{n - i}min  fc={fc + (i % 3)}")
    print("→ la última medición (ahora) dispara el motor. Revisá Alertas Activas: FATIGA, prioridad Media.")


def escenario_sobreesfuerzo(url, key, args):
    fc = args.fc or 185
    act = args.actividad if args.actividad is not None else 0.9
    print(f"CP-E2E-03 — sobreesfuerzo: FC={fc} + actividad={act} (esperado: alerta SOBREESFUERZO / Crítica)")
    p = {"idDispositivo": args.device_id, "timestamp": iso(datetime.now(timezone.utc)),
         "frecuenciaCardiaca": fc, "nivelActividad": act}
    _send(url, key, p, "FC alta + actividad alta")
    print("→ Alertas Activas: SOBREESFUERZO, prioridad Crítica + notificación.")


def escenario_no_asociado(url, key, args):
    print(f"CP-E2E-07 — wearable no asociado (device-id {args.device_id}) "
          f"(esperado: 400 DISPOSITIVO_INVALIDO, sin persistir, auditado)")
    p = {"idDispositivo": args.device_id, "timestamp": iso(datetime.now(timezone.utc)),
         "frecuenciaCardiaca": 80, "nivelActividad": 0.2}
    status, text = _send(url, key, p, "medición de wearable sin asignación vigente")
    if status == 400 and "DISPOSITIVO_INVALIDO" in text:
        print("→ OK: rechazada y auditada (log_auditoria, operacion=DESCARTE_VALIDACION).")
    else:
        print("→ ¡Ojo! ese device-id sí tiene asignación vigente. Usá uno sin asignar.")


def escenario_sin_consentimiento(url, key, args):
    print(f"CP-E2E-08 — sin consentimiento (device-id {args.device_id}) "
          f"(esperado: 403, biodato NO persistido, descarte en memoria sin auditar)")
    p = {"idDispositivo": args.device_id, "timestamp": iso(datetime.now(timezone.utc)),
         "frecuenciaCardiaca": 80, "nivelActividad": 0.2}
    status, text = _send(url, key, p, "medición de operario sin consentimiento vigente")
    if status == 403:
        print("→ OK: ingesta bloqueada por privacidad (Ley 25.326). El biodato no deja rastro.")
    else:
        print("→ ese operario tiene consentimiento vigente. Revocalo primero (Admin→Consentimientos).")


def escenario_invalida(url, key, args):
    print("CP-E2E — paquetes inválidos (RF-04 / H0008; esperado: 400 + auditoría por cada uno)")
    now = iso(datetime.now(timezone.utc))
    base = {"idDispositivo": args.device_id, "timestamp": now, "frecuenciaCardiaca": 80, "nivelActividad": 0.2}
    _send(url, key, {k: v for k, v in base.items() if k != "timestamp"}, "sin timestamp  -> CAMPOS_INCOMPLETOS")
    _send(url, key, {**base, "frecuenciaCardiaca": 250}, "FC=250 fuera de rango -> FUERA_DE_RANGO")
    _send(url, key, {**base, "frecuenciaCardiaca": "ochenta"}, "FC no numérica -> ESTRUCTURA_INVALIDA")
    _send(url, key, None, "JSON corrupto -> ESTRUCTURA_INVALIDA",
          raw=b'{"idDispositivo": 1, "frecuenciaCardiaca": ')
    print("→ ninguna persiste; todas quedan en log_auditoria (operacion=DESCARTE_VALIDACION).")


ESCENARIOS = {
    "normal": escenario_normal,
    "fatiga": escenario_fatiga,
    "sobreesfuerzo": escenario_sobreesfuerzo,
    "no-asociado": escenario_no_asociado,
    "sin-consentimiento": escenario_sin_consentimiento,
    "invalida": escenario_invalida,
}


def main():
    p = argparse.ArgumentParser(description="Reproductor de escenarios E2E de SafePlace")
    p.add_argument("--scenario", required=True, choices=sorted(ESCENARIOS))
    p.add_argument("--backend-url", required=True, help="Base del backend, sin path")
    p.add_argument("--api-key", required=True)
    p.add_argument("--device-id", type=int, required=True)
    p.add_argument("--fc", type=int, default=None, help="FC base del escenario")
    p.add_argument("--actividad", type=float, default=None, help="nivelActividad 0..1")
    p.add_argument("--window-minutes", type=int, default=12, help="fatiga: minutos de ventana sostenida")
    args = p.parse_args()

    url = f"{args.backend_url.rstrip('/')}/api/v1/mediciones"
    print(f"→ endpoint: {url}\n")
    ESCENARIOS[args.scenario](url, args.api_key, args)


if __name__ == "__main__":
    main()
