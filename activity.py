"""
SafePlace — Estimador de nivel de actividad (proxy derivado de FC).

El Garmin Forerunner 265, en modo broadcast de frecuencia cardíaca (BLE
Heart Rate Service), NO expone un nivel de actividad / cadencia / pasos. Para
que el backend pueda evaluar sobreesfuerzo (H0011) y mostrar actividad en el
monitoreo, el gateway estima un `nivelActividad` (escala 0.0–1.0) a partir de
la propia serie de pulsaciones:

  - variabilidad de corto plazo (media de diferencias sucesivas absolutas de
    BPM en una ventana): en reposo el pulso a 1 Hz es estable; el movimiento
    lo hace fluctuar;
  - nivel por encima de la FC de reposo: cuanto más alto el pulso sostenido,
    más probable que haya esfuerzo físico.

El resultado se CUANTIZA a 0.0 cuando cae por debajo de un piso de
movimiento configurable — "prácticamente quieto" — para que el backend pueda
distinguir sin ambigüedad la ausencia de movimiento.

IMPORTANTE: es una aproximación explícita del MVP (ver ADR-14 del Documento
de Arquitectura). NO es el disparador de la alerta de inactividad prolongada
(CP-E2E-04) — esa se define como desconexión del wearable en horario laboral.

Modos (env ACTIVITY_MODE):
  - "hr-proxy" (default): el estimador descripto arriba.
  - "fixed":              valor constante (ACTIVITY_FIXED_VALUE) — para tests
                          deterministas de CP-E2E / demos.
  - "off":                no estima nada (devuelve None; el gateway no manda
                          el campo nivelActividad).
"""

from collections import deque


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def _mean_abs_successive_diff(values):
    if len(values) < 2:
        return 0.0
    diffs = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    return sum(diffs) / len(diffs)


class ActivityEstimator:
    def __init__(
        self,
        mode="hr-proxy",
        window=6,
        movement_floor=0.08,
        resting_hr=65,
        fixed_value=0.0,
        # factores de escala: cuánta variación / cuánto exceso sobre reposo
        # se considera "actividad plena" (=1.0).
        variability_scale=6.0,
        level_scale=60.0,
        weight_variability=0.6,
        weight_level=0.4,
    ):
        self.mode = mode
        self.window = max(2, int(window))
        self.movement_floor = float(movement_floor)
        self.resting_hr = float(resting_hr)
        self.fixed_value = _clamp(float(fixed_value))
        self.variability_scale = float(variability_scale)
        self.level_scale = float(level_scale)
        self.weight_variability = float(weight_variability)
        self.weight_level = float(weight_level)
        self._bpms = deque(maxlen=self.window)

    def update(self, bpm):
        """Registra una nueva lectura de BPM. Devuelve el nivelActividad
        estimado (float 0.0–1.0) o None si el modo es 'off'."""
        if bpm is not None:
            self._bpms.append(float(bpm))

        if self.mode == "off":
            return None
        if self.mode == "fixed":
            return self.fixed_value
        return self._estimate_hr_proxy()

    def _estimate_hr_proxy(self):
        if not self._bpms:
            return 0.0

        variability = _mean_abs_successive_diff(list(self._bpms))
        mean_bpm = sum(self._bpms) / len(self._bpms)
        excess = max(0.0, mean_bpm - self.resting_hr)

        raw = (
            self.weight_variability * (variability / self.variability_scale)
            + self.weight_level * (excess / self.level_scale)
        )
        raw = _clamp(raw)

        if raw < self.movement_floor:
            return 0.0
        return round(raw, 3)


def estimator_from_env(os_environ):
    """Construye un ActivityEstimator leyendo las variables de entorno."""
    mode = os_environ.get("ACTIVITY_MODE", "hr-proxy").strip().lower()
    return ActivityEstimator(
        mode=mode,
        window=int(os_environ.get("ACTIVITY_PROXY_WINDOW", "6")),
        movement_floor=float(os_environ.get("ACTIVITY_MOVEMENT_FLOOR", "0.08")),
        resting_hr=float(os_environ.get("RESTING_HR", "65")),
        fixed_value=float(os_environ.get("ACTIVITY_FIXED_VALUE", "0.0")),
    )
