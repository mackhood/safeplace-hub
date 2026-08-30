"""
CP-E2E-06 — Error de transmisión gateway→backend y reenvío posterior.

Verifica la resiliencia del hub SIN BLE ni red real: la cola local
persistente (`heart_rate_log`, flag `sent`) y el `BackendFlusher`
(`hr_store.py`, sin dependencias de bleak/aiohttp).

- Ante fallo de envío, la medición NO se descarta: queda `sent=0` y se
  reintenta en el próximo flush.
- Al recuperarse la conexión, las pendientes se reenvían.
- Una vez enviada (`sent=1`) no se vuelve a enviar → el backend no recibe
  duplicados desde el hub.
- La cola sobrevive al reinicio del proceso.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hr_store import HRReading, HeartRateStore, BackendFlusher  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    s = HeartRateStore(db_path=str(tmp_path / "safeplace.db"))
    yield s
    s.close()


def _reading(bpm, ts):
    return HRReading(bpm=bpm, timestamp=ts, device_address="AA:BB:CC:DD:EE:01", actividad=0.2)


def _pendientes(store):
    return store.fetch_unsent(limit=1000)


def _sender_falla():
    async def sender(session, reading):
        return False
    return sender


def _sender_ok(contador=None):
    async def sender(session, reading):
        if contador is not None:
            contador["n"] += 1
        return True
    return sender


@pytest.mark.asyncio
async def test_backend_caido_conserva_las_mediciones(store):
    for i in range(5):
        store.save(_reading(80 + i, 1000 + i))

    flusher = BackendFlusher(store, _sender_falla(), enabled=True)
    enviados = await flusher.flush(session=None)

    assert enviados == 0
    assert len(_pendientes(store)) == 5  # nada se descartó


@pytest.mark.asyncio
async def test_reconexion_reenvia_pendientes_una_sola_vez(store):
    for i in range(5):
        store.save(_reading(80 + i, 2000 + i))

    contador = {"n": 0}
    flusher = BackendFlusher(store, _sender_ok(contador), enabled=True)

    enviados = await flusher.flush(session=None)
    assert enviados == 5
    assert len(_pendientes(store)) == 0
    assert contador["n"] == 5

    # segundo flush: nada pendiente -> no se reenvía nada (sin duplicados)
    enviados2 = await flusher.flush(session=None)
    assert enviados2 == 0
    assert contador["n"] == 5


@pytest.mark.asyncio
async def test_fallo_parcial_frena_y_deja_el_resto_en_cola(store):
    for i in range(6):
        store.save(_reading(80 + i, 3000 + i))

    estado = {"n": 0}

    async def intermitente(session, reading):
        estado["n"] += 1
        return estado["n"] <= 2  # los 2 primeros OK, después "cae"

    flusher = BackendFlusher(store, intermitente, enabled=True)
    enviados = await flusher.flush(session=None)
    assert enviados == 2
    assert len(_pendientes(store)) == 4

    flusher2 = BackendFlusher(store, _sender_ok(), enabled=True)
    enviados2 = await flusher2.flush(session=None)
    assert enviados2 == 4
    assert len(_pendientes(store)) == 0


@pytest.mark.asyncio
async def test_flush_desactivado_no_hace_nada(store):
    store.save(_reading(80, 5000))
    flusher = BackendFlusher(store, _sender_ok(), enabled=False)
    assert await flusher.flush(session=None) == 0
    assert len(_pendientes(store)) == 1


def test_persistencia_sobrevive_reinicio_del_proceso(tmp_path):
    db = str(tmp_path / "safeplace.db")
    s1 = HeartRateStore(db_path=db)
    for i in range(3):
        s1.save(_reading(90 + i, 4000 + i))
    s1.close()

    s2 = HeartRateStore(db_path=db)  # "reinicio": nueva instancia, mismo archivo
    assert len(s2.fetch_unsent(limit=100)) == 3
    s2.close()
