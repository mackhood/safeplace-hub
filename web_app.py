#!/usr/bin/env python3
"""SafePlace Hub — Web UI"""

import asyncio
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from bleak import BleakScanner
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

PROJECT_DIR = Path.home() / "safeplace-gateway"
DB_PATH     = os.getenv("DB_PATH", str(PROJECT_DIR / "safeplace.db"))
ENV_PATH    = PROJECT_DIR / ".env"
SERVICE     = "safeplace-gateway"
HR_UUID     = "0000180d-0000-1000-8000-00805f9b34fb"

app = FastAPI()

# ── helpers ──────────────────────────────────────────────────────────────────

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def read_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

def save_targets(targets: list[str]):
    text = ENV_PATH.read_text()
    lines = []
    for line in text.splitlines():
        if line.startswith("TARGET_ADDRESSES="):
            lines.append(f"TARGET_ADDRESSES={','.join(targets)}")
        else:
            lines.append(line)
    ENV_PATH.write_text("\n".join(lines) + "\n")

def restart_gateway():
    subprocess.run(["sudo", "systemctl", "restart", SERVICE], check=True)

def devices_snapshot() -> list[dict]:
    env = read_env()
    targets = [a.strip() for a in env.get("TARGET_ADDRESSES", "").split(",") if a.strip()]
    now = time.time()
    conn = db()
    result = []
    for addr in targets:
        row = conn.execute(
            "SELECT bpm, timestamp FROM heart_rate_log WHERE device_addr=? ORDER BY id DESC LIMIT 1",
            (addr,),
        ).fetchone()
        history = [
            r[0] for r in conn.execute(
                "SELECT bpm FROM heart_rate_log WHERE device_addr=? ORDER BY id DESC LIMIT 20",
                (addr,),
            ).fetchall()
        ]
        history.reverse()
        connected = bool(row and (now - row["timestamp"]) < 15)
        result.append({
            "address":   addr,
            "bpm":       row["bpm"] if row else None,
            "last_seen": row["timestamp"] if row else None,
            "connected": connected,
            "history":   history,
        })
    conn.close()
    return result

# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (PROJECT_DIR / "templates" / "index.html").read_text()

@app.get("/api/devices")
def get_devices():
    return devices_snapshot()

@app.get("/api/stream")
async def stream():
    async def generate():
        while True:
            try:
                data = devices_snapshot()
                yield f"data: {json.dumps(data)}\n\n"
            except Exception:
                pass
            await asyncio.sleep(1)
    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control":    "no-cache",
        "X-Accel-Buffering": "no",
        "Connection":        "keep-alive",
    })

@app.get("/api/scan")
async def scan():
    env = read_env()
    known = {a.strip() for a in env.get("TARGET_ADDRESSES", "").split(",") if a.strip()}
    found = []
    devices = await BleakScanner.discover(timeout=8, return_adv=True)
    for addr, (dev, adv) in devices.items():
        uuids = [str(u).lower() for u in adv.service_uuids]
        if HR_UUID in uuids:
            found.append({
                "address":       addr,
                "name":          dev.name or "Sin nombre",
                "rssi":          adv.rssi,
                "already_added": addr in known,
            })
    return found

@app.post("/api/devices/{address}")
async def add_device(address: str):
    env = read_env()
    targets = [a.strip() for a in env.get("TARGET_ADDRESSES", "").split(",") if a.strip()]
    if address not in targets:
        targets.append(address)
        save_targets(targets)
        restart_gateway()
    return {"targets": targets}

@app.delete("/api/devices/{address}")
async def remove_device(address: str):
    env = read_env()
    targets = [a.strip() for a in env.get("TARGET_ADDRESSES", "").split(",") if a.strip()]
    targets = [t for t in targets if t != address]
    save_targets(targets)
    restart_gateway()
    return {"targets": targets}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
