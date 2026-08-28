#!/usr/bin/env python3
"""
SafePlace — Inyector de mediciones sintéticas (sin BLE).

Postea mediciones directamente al backend con el mismo contrato que usa el
gateway real. Sirve para pruebas end-to-end (CP-E2E-*) sin un wearable
físico: por ejemplo generar una serie de mediciones y luego simular una
desconexión.

Ejemplos:

  # 5 mediciones "normales" cada 10s
  python tools/inject_measurements.py --backend-url "$BACKEND_URL" \
      --api-key "$API_KEY" --device-id 12 --fc 72 --actividad 0.1 \
      --count 5 --interval-seconds 10

  # una sola medición retrodatada 20 minutos (para armar historial)
  python tools/inject_measurements.py --backend-url "$BACKEND_URL" \
      --api-key "$API_KEY" --device-id 12 --fc 70 --backdate-minutes 20 --count 1
"""

import argparse
import sys
import time
import urllib.request
import json
from datetime import datetime, timedelta, timezone


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def post(url: str, api_key: str, payload: dict) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-device-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def main():
    p = argparse.ArgumentParser(description="Inyector de mediciones SafePlace")
    p.add_argument("--backend-url", required=True, help="Base del backend, sin path (ej. https://host)")
    p.add_argument("--api-key", required=True)
    p.add_argument("--device-id", type=int, required=True)
    p.add_argument("--fc", type=int, default=72, help="Frecuencia cardíaca (BPM)")
    p.add_argument("--fc-jitter", type=int, default=0, help="Varía la FC ±jitter por lectura")
    p.add_argument("--actividad", type=float, default=None, help="nivelActividad 0..1 (omitir = no mandar)")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--interval-seconds", type=float, default=5.0, help="Separación entre timestamps")
    p.add_argument("--backdate-minutes", type=float, default=0.0,
                   help="La primera medición arranca N minutos en el pasado")
    p.add_argument("--no-wait", action="store_true",
                   help="No dormir entre requests (solo separa los timestamps)")
    args = p.parse_args()

    url = f"{args.backend_url.rstrip('/')}/api/v1/mediciones"
    base = datetime.now(timezone.utc) - timedelta(minutes=args.backdate_minutes)

    ok = 0
    for i in range(args.count):
        ts = base + timedelta(seconds=i * args.interval_seconds)
        fc = args.fc + (i % (2 * args.fc_jitter + 1) - args.fc_jitter if args.fc_jitter else 0)
        payload = {"idDispositivo": args.device_id, "timestamp": iso(ts), "frecuenciaCardiaca": fc}
        if args.actividad is not None:
            payload["nivelActividad"] = args.actividad

        status, body = post(url, args.api_key, payload)
        marker = "ok" if 200 <= status < 300 else "FALLO"
        print(f"[{i + 1}/{args.count}] {marker} HTTP {status} fc={fc} ts={payload['timestamp']} {body if status >= 300 else ''}")
        if 200 <= status < 300:
            ok += 1

        if not args.no_wait and i < args.count - 1:
            time.sleep(args.interval_seconds)

    print(f"\n{ok}/{args.count} mediciones aceptadas.")
    sys.exit(0 if ok == args.count else 1)


if __name__ == "__main__":
    main()
