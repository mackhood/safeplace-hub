#!/usr/bin/env python3
"""
SafePlace — Lector de heart rate para Xiaomi Smart Band 9 Active.

La Band no expone el servicio estándar de Heart Rate (0x180D): el pulso sólo
sale por el protocolo propietario MiWear SPP v2 sobre BLE, que exige abrir una
sesión y autenticarse antes de responder nada.

El protocolo está documentado byte por byte en
docs/xiaomi-band9-active-protocol.md.

Uso:
    python3 xiaomi_band9.py --once              # una lectura
    python3 xiaomi_band9.py --poll 5            # imprime cada 5s
    python3 xiaomi_band9.py --poll 5 --feed     # además postea al backend
"""

import argparse
import hashlib
import hmac
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import dbus
import dbus.mainloop.glib
from gi.repository import GLib
from Cryptodome.Cipher import AES

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

log = logging.getLogger("xiaomi-band9")

MAC = os.getenv("BAND_MAC", "")
ADAPTER = os.getenv("BAND_ADAPTER", "hci0")

# La auth key es un secreto por dispositivo: quien la tenga puede leer el pulso
# estando en alcance BLE. Va en el .env (ignorado por git), nunca en el código.
_AUTH_KEY_HEX = os.getenv("BAND_AUTH_KEY", "")

# Los paths D-Bus (/serviceXXXX/charYYYY) los asigna BlueZ y difieren entre
# Raspberrys y entre resets del caché GATT, así que se descubren por UUID.
UUID_TX = "0000005f-0000-1000-8000-00805f9b34fb"   # FE95, write-without-response
UUID_RX = "0000005e-0000-1000-8000-00805f9b34fb"   # FE95, notify

FRAME_ACK, FRAME_SESSION, FRAME_DATA = 1, 2, 3
CHANNEL_PROTOBUF = 1
OPCODE_PLAIN, OPCODE_ENCRYPTED = 1, 2

CMD_TYPE_AUTH, CMD_NONCE, CMD_AUTH = 1, 26, 27
CMD_TYPE_HEALTH, CMD_RT_START, CMD_RT_STOP = 8, 45, 46

# Pedido de apertura de sesión: version 1.0.0, maxFrame 0xfc00, txWindow 32,
# timeout 10s. Es constante, con su CRC ya calculado.
SESSION_REQUEST = bytes.fromhex(
    "a5a5020016001d4d0101030001000002020000fc03020020000402001027"
)


# ─── Primitivas del protocolo ─────────────────────────────────────────

def crc16_arc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_frame(ftype: int, seq: int, body: bytes = b"") -> bytes:
    return (bytes([0xA5, 0xA5, ftype, seq])
            + len(body).to_bytes(2, "little")
            + crc16_arc(body).to_bytes(2, "little")
            + body)


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | 0x80]) if n else bytes([b])
        if not n:
            return out


def pb_varint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def pb_bytes(field: int, data: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(data)) + data


def pb_parse(buf: bytes) -> dict:
    out, i = {}, 0
    while i < len(buf):
        tag, shift = 0, 0
        while True:
            if i >= len(buf):
                return out
            b = buf[i]; i += 1
            tag |= (b & 0x7F) << shift; shift += 7
            if not b & 0x80:
                break
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            val, shift = 0, 0
            while True:
                if i >= len(buf):
                    return out
                b = buf[i]; i += 1
                val |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
            out[field] = val
        elif wire == 2:
            ln, shift = 0, 0
            while True:
                if i >= len(buf):
                    return out
                b = buf[i]; i += 1
                ln |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
            if i + ln > len(buf):
                return out
            out[field] = buf[i:i + ln]; i += ln
        else:
            return out
    return out


def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def derive_keys(auth_key: bytes, phone_nonce: bytes, watch_nonce: bytes):
    """Expansión HKDF-like con "miwear-auth". Devuelve (dec_key, enc_key, dec_nonce, enc_nonce)."""
    root = _hmac(phone_nonce + watch_nonce, auth_key)
    out, prev, counter = b"", b"", 1
    while len(out) < 64:
        prev = _hmac(root, prev + b"miwear-auth" + bytes([counter]))
        out += prev
        counter += 1
    return out[0:16], out[16:32], out[32:36], out[36:40]


class AuthError(RuntimeError):
    pass


class Band9:
    def __init__(self, mac: str, auth_key: bytes, adapter: str = ADAPTER):
        self.mac = mac
        self.auth_key = auth_key
        self.dev_path = f"/org/bluez/{adapter}/dev_" + mac.replace(":", "_")
        self.bus = dbus.SystemBus()
        self.tx_path = self.rx_path = None
        self._buf = bytearray()
        self._frames = []
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._seq = 0
        self.dec_key = self.enc_key = self.dec_nonce = self.enc_nonce = None

    # ── GATT ──
    def _gatt(self, path):
        return dbus.Interface(self.bus.get_object("org.bluez", path),
                              "org.bluez.GattCharacteristic1")

    def _props(self, path):
        return dbus.Interface(self.bus.get_object("org.bluez", path),
                              "org.freedesktop.DBus.Properties")

    def _device(self):
        return dbus.Interface(self.bus.get_object("org.bluez", self.dev_path),
                              "org.bluez.Device1")

    def _dev_prop(self, name):
        try:
            return self._props(self.dev_path).Get("org.bluez.Device1", name)
        except dbus.DBusException:
            return None

    def ensure_connected(self, timeout=30):
        """La Band bondeada no advierte, así que se conecta por path D-Bus, no por scan."""
        if self._dev_prop("Address") is None:
            raise AuthError(
                f"{self.mac} no figura en BlueZ. ¿Falta emparejar? "
                f"Corré tools/pair_band9.py")

        if not bool(self._dev_prop("Connected")):
            log.info("Conectando a %s...", self.mac)
            self._device().Connect()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if bool(self._dev_prop("ServicesResolved")):
                return
            time.sleep(0.5)
        raise AuthError("timeout esperando ServicesResolved")

    def resolve_chars(self):
        """Ubica TX/RX por UUID: los paths de BlueZ cambian entre equipos."""
        mgr = dbus.Interface(self.bus.get_object("org.bluez", "/"),
                             "org.freedesktop.DBus.ObjectManager")
        for path, ifaces in mgr.GetManagedObjects().items():
            char = ifaces.get("org.bluez.GattCharacteristic1")
            if not char or not path.startswith(self.dev_path + "/"):
                continue
            uuid = str(char.get("UUID", "")).lower()
            if uuid == UUID_TX:
                self.tx_path = path
            elif uuid == UUID_RX:
                self.rx_path = path
        if not (self.tx_path and self.rx_path):
            raise AuthError("no encontré las characteristics FE95 005E/005F")
        log.debug("TX=%s  RX=%s", self.tx_path, self.rx_path)

    def _write(self, data: bytes):
        self._gatt(self.tx_path).WriteValue(dbus.Array(data, signature="y"), {})

    def _on_value(self, _iface, changed, _inv):
        if "Value" not in changed:
            return
        self._buf.extend(bytes(changed["Value"]))
        while True:
            start = self._buf.find(b"\xa5\xa5")
            if start < 0 or len(self._buf) < start + 8:
                return
            ln = int.from_bytes(self._buf[start + 4:start + 6], "little")
            if len(self._buf) < start + 8 + ln:
                return
            frame = bytes(self._buf[start:start + 8 + ln])
            del self._buf[:start + 8 + ln]
            self._handle(frame)

    def _handle(self, frame: bytes):
        ftype, seq = frame[2] & 0xF, frame[3]
        ln = int.from_bytes(frame[4:6], "little")
        body = frame[8:8 + ln]
        if crc16_arc(body) != int.from_bytes(frame[6:8], "little"):
            log.warning("CRC inválido, frame descartado")
            return
        with self._lock:
            self._frames.append((ftype, seq, body))
        if ftype == FRAME_DATA:
            self._write(build_frame(FRAME_ACK, seq))
        self._event.set()

    def _send_data(self, body: bytes):
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        self._write(build_frame(FRAME_DATA, seq, body))

    def _wait_data(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for ftype, _seq, body in self._frames:
                    if ftype == FRAME_DATA:
                        return body
            self._event.clear()
            self._event.wait(0.3)
        return None

    def _clear(self):
        with self._lock:
            self._frames.clear()

    # ── Cifrado post-auth: AES-CTR con la clave usada también como IV ──
    def _encrypt(self, msg: bytes) -> bytes:
        return AES.new(self.enc_key, AES.MODE_CTR,
                       nonce=b"", initial_value=self.enc_key).encrypt(msg)

    def _decrypt(self, ct: bytes) -> bytes:
        return AES.new(self.dec_key, AES.MODE_CTR,
                       nonce=b"", initial_value=self.dec_key).decrypt(ct)

    # ── Handshake ──
    def connect(self):
        self.ensure_connected()
        self.resolve_chars()

        self._props(self.rx_path).connect_to_signal("PropertiesChanged", self._on_value)
        self._gatt(self.rx_path).StartNotify()
        time.sleep(1.2)

        self._write(SESSION_REQUEST)
        time.sleep(3.0)

        phone_nonce = os.urandom(16)
        self._clear()
        self._send_data(
            bytes([CHANNEL_PROTOBUF, OPCODE_PLAIN])
            + pb_varint(1, CMD_TYPE_AUTH) + pb_varint(2, CMD_NONCE)
            + pb_bytes(3, pb_bytes(30, pb_bytes(1, phone_nonce))))

        data = self._wait_data(15)
        if data is None:
            raise AuthError("la Band no respondió al CMD_NONCE")

        watch = pb_parse(pb_parse(pb_parse(data[2:])[3])[31])
        watch_nonce, watch_hmac = watch[1], watch[2]

        self.dec_key, self.enc_key, self.dec_nonce, self.enc_nonce = derive_keys(
            self.auth_key, phone_nonce, watch_nonce)

        # La Band firma con la misma clave derivada: si no coincide, la auth key
        # es incorrecta y no tiene sentido seguir.
        if _hmac(self.dec_key, watch_nonce + phone_nonce) != watch_hmac:
            raise AuthError("hmac de la Band no coincide — auth key incorrecta")

        # El deviceInfo va cifrado con AES-CCM (distinto del CTR post-auth);
        # su contenido es irrelevante pero el campo debe existir y estar cifrado.
        device_info = (pb_varint(1, 0) + pb_varint(2, 34)
                       + pb_bytes(3, b"SafePlaceHub")
                       + pb_varint(4, 224) + pb_bytes(5, b"ES"))
        ccm_nonce = self.enc_nonce + b"\x00" * 4 + (0).to_bytes(4, "little")
        ct, mac = AES.new(self.enc_key, AES.MODE_CCM,
                          nonce=ccm_nonce, mac_len=4).encrypt_and_digest(device_info)

        proof = _hmac(self.enc_key, phone_nonce + watch_nonce)

        self._clear()
        self._send_data(
            bytes([CHANNEL_PROTOBUF, OPCODE_PLAIN])
            + pb_varint(1, CMD_TYPE_AUTH) + pb_varint(2, CMD_AUTH)
            + pb_bytes(3, pb_bytes(32, pb_bytes(1, proof) + pb_bytes(2, ct + mac))))

        resp = self._wait_data(18)
        if resp is None:
            raise AuthError("sin respuesta al CMD_AUTH")
        cmd = pb_parse(resp[2:])
        if cmd.get(2) != CMD_AUTH:
            raise AuthError(f"auth rechazada: {cmd}")
        log.info("Autenticado con la Band")

    def read_hr(self, timeout=12):
        """Devuelve los bpm actuales, o None. La Band responde una muestra por pedido."""
        self._clear()
        self._send_data(bytes([CHANNEL_PROTOBUF, OPCODE_ENCRYPTED])
                        + self._encrypt(pb_varint(1, CMD_TYPE_HEALTH)
                                        + pb_varint(2, CMD_RT_START)))
        body = self._wait_data(timeout)
        if body is None:
            return None
        payload = self._decrypt(body[2:]) if body[1] == OPCODE_ENCRYPTED else body[2:]
        cmd = pb_parse(payload)
        if cmd.get(1) != CMD_TYPE_HEALTH:
            return None
        stats = pb_parse(pb_parse(cmd.get(10, b"")).get(39, b""))
        hr = stats.get(4)
        # Gadgetbridge descarta <= 10 como lectura inválida (sin contacto).
        return hr if hr and hr > 10 else None

    def stop(self):
        try:
            self._send_data(bytes([CHANNEL_PROTOBUF, OPCODE_ENCRYPTED])
                            + self._encrypt(pb_varint(1, CMD_TYPE_HEALTH)
                                            + pb_varint(2, CMD_RT_STOP)))
        except Exception:
            pass


# ─── Envío al backend ─────────────────────────────────────────────────

def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


class Backend:
    def __init__(self, base_url: str, api_key: str, mac: str):
        import requests
        self._requests = requests
        self.base = base_url.rstrip("/")
        self.headers = {"x-device-api-key": api_key}
        self.mac = mac
        self.device_id = None

    def resolve(self):
        r = self._requests.get(f"{self.base}/api/v1/dispositivos/lookup",
                               params={"mac": self.mac}, headers=self.headers, timeout=10)
        r.raise_for_status()
        self.device_id = (r.json().get("data") or {}).get("id")
        log.info("Dispositivo resuelto: id=%s", self.device_id)
        return self.device_id

    def post(self, bpm: int):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        r = self._requests.post(
            f"{self.base}/api/v1/mediciones",
            json={"idDispositivo": self.device_id, "timestamp": ts, "frecuenciaCardiaca": bpm},
            headers=self.headers, timeout=10)
        if r.status_code >= 300:
            log.warning("Backend HTTP %d: %s", r.status_code, r.text[:200])
            return False
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=int, metavar="SEG")
    ap.add_argument("--feed", action="store_true", help="postear al backend")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    load_env(Path(__file__).resolve().parent.parent / ".env")

    mac = os.getenv("BAND_MAC", MAC)
    key_hex = os.getenv("BAND_AUTH_KEY", _AUTH_KEY_HEX)
    if not mac or not key_hex:
        log.error("Faltan BAND_MAC y/o BAND_AUTH_KEY en el .env. "
                  "Ver docs/provisioning-raspberry.md para obtener la auth key.")
        return 1
    try:
        auth_key = bytes.fromhex(key_hex)
    except ValueError:
        log.error("BAND_AUTH_KEY no es hexadecimal válido")
        return 1

    backend = None
    if args.feed:
        url, api_key = os.getenv("BACKEND_URL", ""), os.getenv("API_KEY", "")
        if not url:
            log.error("--feed necesita BACKEND_URL en el .env")
            return 1
        backend = Backend(url, api_key, mac)

    band = Band9(mac, auth_key)
    loop = GLib.MainLoop()
    rc = [0]

    def work():
        try:
            band.connect()
            if backend and backend.resolve() is None:
                log.error("La MAC %s no está cargada en el backend", mac)
                rc[0] = 1
                return

            interval = args.poll or 0
            while True:
                bpm = band.read_hr()
                if bpm is None:
                    log.warning("sin lectura")
                else:
                    sent = backend.post(bpm) if backend else None
                    log.info("HR %d bpm%s", bpm,
                             "" if sent is None else ("  → backend OK" if sent else "  → backend FALLÓ"))
                if not interval:
                    break
                time.sleep(interval)
        except AuthError as e:
            log.error("Auth: %s", e)
            rc[0] = 1
        except KeyboardInterrupt:
            pass
        except Exception as e:
            log.exception("Error: %s", e)
            rc[0] = 1
        finally:
            band.stop()
            loop.quit()

    threading.Thread(target=work, daemon=True).start()
    try:
        loop.run()
    except KeyboardInterrupt:
        band.stop()
    return rc[0]


if __name__ == "__main__":
    sys.exit(main())
