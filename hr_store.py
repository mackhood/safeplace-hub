"""
Capa de persistencia y cola de reenvío del gateway — sin dependencias de BLE
ni de red (para poder testearla en cualquier plataforma).

- `HeartRateStore`: SQLite. Todo reading se guarda; `sent=0` = pendiente de
  enviar al backend, `sent=1` = ya enviado, `sent=2` = descartado por TTL
  (más viejo que BUFFER_TTL sin poder enviarse). Nunca se borra una fila.
- `BackendFlusher`: usa la tabla como cola. El envío real se inyecta como
  callable (`sender`), así el flusher no conoce aiohttp ni el endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ble-gateway")

DEFAULT_BATCH_SIZE = 50


@dataclass
class HRReading:
    bpm: int
    timestamp: float
    device_address: str
    actividad: float | None = None


class HeartRateStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
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
        # Migración in-place (SQLite no tiene ADD COLUMN IF NOT EXISTS).
        try:
            self._conn.execute("ALTER TABLE heart_rate_log ADD COLUMN actividad REAL")
        except sqlite3.OperationalError:
            pass
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

    def fetch_unsent(self, limit: int = DEFAULT_BATCH_SIZE) -> list:
        return self._conn.execute(
            "SELECT id, bpm, timestamp, device_addr, actividad "
            "FROM heart_rate_log WHERE sent=0 ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def purge_stale(self, ttl_seconds: int) -> int:
        """Marca (sent=2) las lecturas pendientes más viejas que `ttl_seconds`.

        Sin esto, el buffer offline crece sin límite y reenvía lecturas de
        hace días en cada reinicio del gateway — el backend las deduplica pero
        quedan auditadas como descarte. `BUFFER_TTL` (default 2 h) acota cuánto
        tiene sentido reintentar un tramo caído antes de darlo por perdido.
        """
        if ttl_seconds <= 0:
            return 0
        cur = self._conn.execute(
            "UPDATE heart_rate_log SET sent=2 "
            "WHERE sent=0 AND created_at < strftime('%s','now') - ?",
            (ttl_seconds,),
        )
        self._conn.commit()
        if cur.rowcount:
            log.info("Buffer: %d lectura(s) pendiente(s) descartada(s) por TTL (%ds)", cur.rowcount, ttl_seconds)
        return cur.rowcount

    def close(self):
        self._conn.close()


class BackendFlusher:
    """
    Cola de reenvío sobre heart_rate_log (sent=0).

    `sender` es `async (session, HRReading) -> bool`: True = enviado OK.
    Ante el primer fallo se corta el batch y las pendientes quedan para el
    próximo flush (CP-E2E-06). Una vez `sent=1`, nunca se reenvía → el
    backend no recibe duplicados desde el hub.
    """

    def __init__(self, store: HeartRateStore, sender, *, enabled: bool = True,
                 batch_size: int = DEFAULT_BATCH_SIZE, ttl_seconds: int = 0):
        self._store = store
        self._sender = sender
        self._enabled = enabled
        self._batch_size = batch_size
        self._ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()

    async def flush(self, session) -> int:
        if not self._enabled or self._lock.locked():
            return 0

        async with self._lock:
            self._store.purge_stale(self._ttl_seconds)
            total_sent = 0
            while True:
                batch = self._store.fetch_unsent(self._batch_size)
                if not batch:
                    break

                sent_ids = []
                for row_id, bpm, ts, addr, actividad in batch:
                    reading = HRReading(bpm=bpm, timestamp=ts, device_address=addr, actividad=actividad)
                    ok = await self._sender(session, reading)
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
