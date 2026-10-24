# Levantar un hub SafePlace en una Raspberry desde cero

Guía completa para dejar una Raspberry Pi leyendo pulso de un wearable y
reportándolo al backend de SafePlace.

Un hub puede leer **wearables con servicio HR estándar** (Garmin, iGPSPORT) o
una **Xiaomi Smart Band 9 Active**, que usa un protocolo propietario. Qué lee
cada Pi se decide con una sola variable: `HUB_MODE`.

---

## 0. Panorama

```
   wearable  ──BLE──▶  Raspberry Pi  ──HTTPS──▶  backend  ──▶  Neon (Postgres)
                       (este repo)                                    │
                                                                      ▼
                                                                  plataforma
```

El hub lee el pulso, resuelve a qué dispositivo corresponde la MAC y postea
mediciones. **No decide de qué operario son**: eso lo resuelve el backend
cruzando la MAC con la asignación vigente (§7).

| `HUB_MODE` | Lector | Wearables | Intérprete |
|-----------|--------|-----------|-----------|
| `garmin` | `ble_gateway.py` | Los que exponen el servicio HR estándar `0x180D` | venv (bleak, aiohttp) |
| `xiaomi` | `tools/xiaomi_band9.py` | Xiaomi Smart Band 9 Active | Python del sistema (dbus-python, GLib) |
| `both` | los dos | — | ambos |

Los intérpretes son distintos a propósito: el lector de la Xiaomi necesita
`python3-dbus` y `python3-gi`, que son paquetes del sistema y no se instalan
bien dentro de un venv. `hub_entrypoint.sh` se encarga de elegir.

---

## 1. Hardware

- Raspberry Pi 4 o 5.
- **Dongle BLE USB.** Recomendado. En la Pi de referencia el adaptador interno
  quedó como `hci1` (DOWN) y el dongle USB como `hci0` (UP), y el hub usa
  `hci0`. Si sólo vas a usar el adaptador interno, fijate con `hciconfig` cuál
  queda activo y ajustá `BAND_ADAPTER` en el `.env`.
- El wearable.

Verificá qué adaptadores ve el sistema:

```bash
hciconfig
```

```
hci1:   Type: Primary  Bus: UART     <- interno
        DOWN
hci0:   Type: Primary  Bus: USB      <- dongle, el que se usa
        UP RUNNING
```

---

## 2. Sistema operativo y acceso

Raspberry Pi OS (Bookworm o posterior). Con SSH habilitado alcanza.

La Pi de referencia se accede por Ethernet directo desde una notebook Windows
con *Internet Connection Sharing*, lo que le asigna una IP fija del rango
`192.168.137.x`. Cualquier forma de llegar por red sirve; lo único que importa
es que la Pi tenga salida a Internet para alcanzar el backend.

```bash
ssh usuario@<ip-de-la-pi>
```

---

## 3. Clonar y provisionar

```bash
git clone https://github.com/mackhood/safeplace-hub.git ~/safeplace-hub
cd ~/safeplace-hub
bash setup_hub.sh
```

`setup_hub.sh` es idempotente y hace:

1. Instala dependencias de sistema (`bluez`, `python3-dbus`, `python3-gi`,
   `python3-pycryptodome`, `python3-requests`, headers de build).
2. Crea el venv con `bleak` y `aiohttp`.
3. Copia `.env.example` a `.env` si no existe.
4. Deja Bluetooth bien configurado (§4).
5. Instala y habilita el servicio `safeplace-hub`, ajustando rutas y usuario.

> **No uses `setup_gateway.sh`**: quedó obsoleto y creaba un servicio distinto
> (`safeplace-gateway`) en otro directorio con una copia vieja del gateway
> embebida. Fue reemplazado por `setup_hub.sh`.

---

## 4. Bluetooth

El script ya lo deja listo, pero conviene entender qué toca.

**`AutoEnable=true`** en `/etc/bluetooth/main.conf`, bajo `[Policy]`. Sin esto
el adaptador queda `DOWN` después de cada reboot y el hub no arranca hasta que
alguien entra a mano y hace `bluetoothctl power on`.

```ini
[Policy]
AutoEnable=true
```

**`rfkill`**: si el radio quedó bloqueado por software, nada funciona y el
error no es evidente.

```bash
rfkill list bluetooth
sudo rfkill unblock bluetooth
```

Comprobación final:

```bash
hciconfig           # el adaptador que vas a usar debe decir UP RUNNING
systemctl status bluetooth
```

---

## 5. Emparejar el wearable

### 5.a — Modo `garmin` (servicio HR estándar)

No hace falta bonding. Basta con poner la MAC en el `.env`:

```bash
TARGET_ADDRESSES=A0:28:84:65:BE:F8
```

Para encontrarla, con el wearable en modo visible:

```bash
bluetoothctl --timeout 15 scan on
```

Si dejás `TARGET_ADDRESSES` vacío, el gateway escanea y se conecta a todos los
dispositivos que expongan `0x180D`.

### 5.b — Modo `xiaomi` (Band 9 Active)

Acá hay dos requisitos que no son obvios y son la causa más común de que no
funcione.

**Requisito 1 — bonding.** Sin bond, *toda* operación GATT contra la Band
falla con `Unlikely Error (0x0E)`. Se hace una sola vez por Raspberry:

```bash
cd ~/safeplace-hub
venv/bin/python tools/pair_band9.py --mac B8:53:84:31:A0:9C
```

Verificá:

```bash
bluetoothctl info <MAC>     # Paired: yes / Bonded: yes / Trusted: yes
```

> Una vez bondeada, **la Band deja de advertir**. A partir de ahí un scan por
> MAC ya no la encuentra; se conecta por path D-Bus (el lector lo hace solo) o
> con `bluetoothctl connect <MAC>`. Si necesitás volver a emparejar, primero
> `bluetoothctl remove <MAC>`.

**Requisito 2 — la auth key.** Es un secreto de 16 bytes, distinto por
dispositivo, que la Band exige en el handshake. Sin la correcta, el
handshake devuelve `status 7` y no hay pulso.

Se obtiene al vincular la Band con la app oficial **Mi Fitness** o con
**Gadgetbridge**; es el valor que esas apps guardan como *auth key* / *token*
del dispositivo. Una vez que la tenés:

```bash
BAND_MAC=B8:53:84:31:A0:9C
BAND_AUTH_KEY=<32 caracteres hex>
```

> ⚠️ **Nunca subas la auth key al repo.** Con esa clave y estando en alcance
> BLE, cualquiera puede leer el pulso de quien lleve la Band. El `.env` está en
> `.gitignore` justamente por eso.

El protocolo completo, por si hay que depurar, está en
[xiaomi-band9-active-protocol.md](xiaomi-band9-active-protocol.md).

---

## 6. Configurar el hub

```bash
nano ~/safeplace-hub/.env
```

Mínimo indispensable:

```bash
HUB_MODE=xiaomi                 # o garmin, o both
BACKEND_URL=https://tu-backend.onrender.com    # URL base, SIN path
API_KEY=<igual a GATEWAY_API_KEY del backend>

# según el modo:
TARGET_ADDRESSES=<MAC>          # garmin
BAND_MAC=<MAC>                  # xiaomi
BAND_AUTH_KEY=<hex>             # xiaomi
```

`API_KEY` viaja en el header `x-device-api-key` y tiene que coincidir **exacto**
con `GATEWAY_API_KEY` del backend. Si no coincide, el hub lee pulso pero el
backend rechaza todo y en el log vas a ver `HTTP 401`.

---

## 7. Dar de alta el wearable en el backend

**Este paso es imprescindible.** Si la MAC no está cargada, el hub lee pulso
pero descarta cada medición con
`Sin dispositivo asociado en el backend — medición no enviada`.

El backend no tiene endpoint para crear dispositivos: se hace en la base.

```sql
-- 1) el dispositivo
INSERT INTO dispositivo (marca, modelo, estado, direccion_mac)
VALUES ('Xiaomi', 'Smart Band 9 Active', true, 'B8:53:84:31:A0:9C')
RETURNING id;

-- 2) asignarlo a un operario (cerrando antes cualquier asignación vigente
--    suya, para que un simulador viejo no le pise el pulso real)
UPDATE asignacion_dispositivo SET fecha_hasta = now()
WHERE id_trabajador = <ID_OPERARIO>
  AND (fecha_hasta IS NULL OR fecha_hasta >= now());

INSERT INTO asignacion_dispositivo (id_trabajador, id_dispositivo, fecha_desde, fecha_hasta)
VALUES (<ID_OPERARIO>, <ID_DISPOSITIVO>, now(), NULL);
```

Dos trampas del esquema que cuestan tiempo:

- La tabla de operarios se llama **`operario`**, no `trabajador`. Pero
  `asignacion_dispositivo` referencia la columna **`id_trabajador`**.
- Para que el operario figure **en turno**, necesita una fila en
  `horario_operario` para el día de la semana actual cuya ventana incluya la
  hora. La consulta usa `BETWEEN hora_inicio AND hora_fin`, así que **una
  ventana que cruza medianoche (22:00–06:00) nunca matchea**: un turno noche
  tiene que ser una ventana del mismo día, o una excepción con `fecha`.

---

## 8. Arrancar y verificar

```bash
sudo systemctl start safeplace-hub
journalctl -u safeplace-hub -f
```

En modo `xiaomi` un arranque sano se ve así:

```
[hub] modo xiaomi — /usr/bin/python3 tools/xiaomi_band9.py
Autenticado con la Band
Dispositivo resuelto: id=32
HR 85 bpm  → backend OK
HR 87 bpm  → backend OK
```

Prueba manual, sin systemd y sin tocar el backend:

```bash
cd ~/safeplace-hub
python3 tools/xiaomi_band9.py --once          # una lectura, sólo imprime
python3 tools/xiaomi_band9.py --poll 5        # loop, sólo imprime
python3 tools/xiaomi_band9.py --once --feed   # una lectura y la postea
```

Comandos de operación:

```bash
sudo systemctl restart safeplace-hub
sudo systemctl stop safeplace-hub
sudo systemctl status safeplace-hub
journalctl -u safeplace-hub --since '10 min ago'
```

---

## 9. Problemas frecuentes

**El servicio arranca y se cae en loop.**
`journalctl -u safeplace-hub -n 50`. Lo más común: falta `BAND_AUTH_KEY` o
`BAND_MAC`, o el adaptador está DOWN.

**`no figura en BlueZ. ¿Falta emparejar?`**
La Band nunca se bondeó en *esta* Raspberry. El bond es por equipo: emparejarla
en otra Pi no sirve. Correr `tools/pair_band9.py`.

**La Band no aparece en el scan.**
Si ya está bondeada, es lo esperado: deja de advertir. Para re-emparejar,
`bluetoothctl remove <MAC>` primero.

**`failed to discover services, device disconnected` repetido.**
Caché GATT de BlueZ desincronizado. Se destraba forzando un descubrimiento
limpio:

```bash
bluetoothctl remove <MAC>
sudo systemctl restart bluetooth
```

(En modo `garmin`, `ble_gateway.py` ya hace esto solo como watchdog.)

**El hub lee pulso pero no aparece nada en la plataforma.**
Tres causas, en orden de probabilidad:
1. La MAC no está cargada en el backend → §7.
2. `API_KEY` no coincide con `GATEWAY_API_KEY` → `HTTP 401` en el log.
3. El operario está fuera de su ventana de `horario_operario` → §7.

**Los dos modos peleándose por el adaptador.**
`garmin` y `xiaomi` usan el mismo `hci0`. `HUB_MODE=both` funciona, pero si
además dejaste algún proceso suelto de pruebas, matalo:

```bash
pgrep -af 'xiaomi_band9|ble_gateway'
```

**`pkill -f <nombre>` por SSH te corta la sesión.**
El patrón matchea la línea de comando del propio shell que lo ejecuta y se mata
a sí mismo (`exit 255`). Usá `pkill -x` (nombre exacto) o un patrón que no se
auto-matchee, como `pkill -f '[x]iaomi_band9'`.

**Depurar BLE a bajo nivel.**
Capturar el tráfico HCI muestra si los bytes salen al aire y qué contesta el
dispositivo:

```bash
sudo setsid nohup btmon -i hci0 -w /tmp/cap.btsnoop </dev/null >/dev/null 2>&1 &
# ... reproducir el problema ...
sudo pkill -x btmon
sudo btmon -T -r /tmp/cap.btsnoop > /tmp/cap.txt
```

Buscar en `cap.txt`: `Write Request`, `Handle Value Notification`,
`Error Response`, `Disconnect Complete`.

---

## 10. Mapa del repo

| Archivo | Qué es |
|---------|--------|
| `hub_entrypoint.sh` | Elige el lector según `HUB_MODE`. Es el `ExecStart` del servicio. |
| `ble_gateway.py` | Lector de wearables con servicio HR estándar. |
| `tools/xiaomi_band9.py` | Lector de la Xiaomi Band 9 Active (MiWear SPP v2). |
| `tools/pair_band9.py` | Bonding de la Band, se corre una vez por Raspberry. |
| `setup_hub.sh` | Provisioning completo de una Pi. |
| `systemd/safeplace-hub.service` | Plantilla de la unit. |
| `.env.example` | Plantilla de configuración. |
| `docs/xiaomi-band9-active-protocol.md` | El protocolo de la Band, byte por byte. |
