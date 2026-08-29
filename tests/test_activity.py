"""Tests del estimador de actividad (proxy derivado de FC) — CP-E2E-04 / ADR-14."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from activity import ActivityEstimator, estimator_from_env  # noqa: E402


def _feed(est, bpms):
    resultado = None
    for b in bpms:
        resultado = est.update(b)
    return resultado


def test_pulso_estable_en_reposo_da_cero():
    est = ActivityEstimator(mode="hr-proxy", window=6, resting_hr=65, movement_floor=0.08)
    # FC de reposo, sin variación -> por debajo del piso de movimiento
    assert _feed(est, [64, 64, 64, 64, 64, 64]) == 0.0


def test_pulso_con_saltos_da_actividad_positiva():
    est = ActivityEstimator(mode="hr-proxy", window=6, resting_hr=65, movement_floor=0.08)
    valor = _feed(est, [70, 95, 78, 110, 85, 120])
    assert valor is not None and valor > 0.0


def test_pulso_alto_sostenido_da_actividad():
    est = ActivityEstimator(mode="hr-proxy", window=6, resting_hr=65, movement_floor=0.08)
    valor = _feed(est, [150, 151, 150, 152, 151, 150])
    assert valor > 0.0


def test_resultado_siempre_en_rango():
    est = ActivityEstimator(mode="hr-proxy", window=4, resting_hr=60)
    for valor in [_feed(est, [40, 200, 40, 200]), _feed(est, [200, 40, 200, 40])]:
        assert 0.0 <= valor <= 1.0


def test_modo_fixed():
    est = ActivityEstimator(mode="fixed", fixed_value=0.0)
    assert est.update(80) == 0.0
    est2 = ActivityEstimator(mode="fixed", fixed_value=0.9)
    assert est2.update(80) == 0.9


def test_modo_off_devuelve_none():
    est = ActivityEstimator(mode="off")
    assert est.update(80) is None


def test_estimator_from_env_defaults():
    est = estimator_from_env({})
    assert est.mode == "hr-proxy"
    est2 = estimator_from_env({"ACTIVITY_MODE": "fixed", "ACTIVITY_FIXED_VALUE": "0.5"})
    assert est2.update(70) == 0.5
