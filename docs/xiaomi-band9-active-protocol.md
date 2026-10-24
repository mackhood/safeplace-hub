# Xiaomi Smart Band 9 Active — Protocolo BLE (ingeniería inversa)

Notas de la integración de la Band 9 Active como fuente de HR en el hub de SafePlace.
Todo lo que figura acá está **verificado contra el dispositivo real**.

**Estado: resuelto.** Lectura de heart rate funcionando end-to-end.

Última actualización: 2026-10-23

---

## 1. Dispositivo

```
MAC          <BAND_MAC>           ej. B8:53:84:31:A0:9C
Nombre       Xiaomi Band 9 Active <últimos 4 de la MAC>
Modelo       miwear.watch.n69gl
Model Number "leopard"            (GATT 0x2a24)
Auth key     <BAND_AUTH_KEY>      16 bytes hex, por dispositivo
Adaptador    hci0 (dongle USB; ver docs/provisioning-raspberry.md)
```

> **La auth key es un secreto.** Quien la tenga puede leer el pulso del portador
> estando en alcance BLE. Va en el `.env` (ignorado por git), nunca en el código
> ni en este documento. Cómo obtenerla: ver
> [provisioning-raspberry.md](provisioning-raspberry.md).

La Band **no** usa el protocolo Huami/Mi Band 2-6. Usa **MiWear SPP v2 sobre BLE**
(lo que Gadgetbridge llama "BLE V2" / dispositivos Xiaomi protobuf).

### Prerrequisito: bonding

Sin bonding, toda operación GATT falla con `0x0E "Unlikely Error"`. Hay que emparejar
una vez (`bleak`'s `client.pair()` o `bluetoothctl pair`) hasta llegar a:

```
Paired: yes   Bonded: yes   Trusted: yes
```

Ojo: **después del bonding la Band deja de advertir**, así que `bleak` no la encuentra
por MAC en un scan. Conectar con `bluetoothctl connect B8:53:84:31:A0:9C` o ir directo
por D-Bus.

---

## 2. Transporte

El canal de aplicación es el **servicio `FE95`**:

| Rol | UUID | D-Bus (bajo `/org/bluez/hci0/dev_B8_53_84_31_A0_9C`) | Handle ATT | Flags |
|-----|------|------------------------------------------------------|-----------|-------|
| **TX** | `0000005f-0000-1000-8000-00805f9b34fb` | `/service002b/char0031` | `0x0032` | write-without-response |
| **RX** | `0000005e-0000-1000-8000-00805f9b34fb` | `/service002b/char002e` | `0x002f`, CCCD `0x0030` | write-without-response, notify |

MTU negociado: **247** (payload máximo 244 B).

### Callejones sin salida (no perder tiempo acá)

- **`FDAB` / `00000002` / `00000003`** (`/service0033/char0036`, `char0039`): canal Xiaomi
  "main" pero **no responde nada** al auth. Se le escribieron 5 formatos distintos de
  CMD_NONCE con notify activo en los tres canales: writes aceptados, cero notificaciones,
  sin error y sin desconexión. La Band recibe los bytes, los ACKea a nivel ATT y los
  descarta en silencio.
- **`E49A25F8` / `E49A25E0` / `E49A28E1`** (`/service004b`): servicio de compatibilidad
  Huami. Mandarle el auth viejo (`01 00 02 80`) hace que la Band **se desconecte a los ~12 s**.
- **`FE95` / `00000050`** lee `030009`. **`FDAB` / `00000001`** lee `01`.
- No existe el servicio estándar de Heart Rate (`0x180D`). El HR sólo sale por MiWear.

### Diagnóstico ya descartado

Vale la pena dejarlo escrito porque costó varias horas:

- `StartNotify` **funciona**. Verificado leyendo el descriptor `0x2902` de vuelta desde el
  dispositivo con notify activo (`Notifying=1`, `CCCD=0100`) y confirmado en `btmon`:
  `ATT: Write Request (0x12) Handle 0x0030 Data 0100` → `ATT: Write Response (0x13)`.
- El **bug de BlueZ #2383** (CCCD inferido) **no aplica**: los `0x2902` son descriptors
  descubiertos de verdad, con handle propio y objeto D-Bus.
- **Write Command (0x52) vs Write Request (0x12)** no importa. La Band acepta ambos; de
  hecho responde `Write Response` a un `0x12` sobre una char write-without-response pura,
  en lugar de `Error Response 0x03`.
- **MTU** no es el problema (247, sobra).
- Un `ATT: Write Response` sólo significa "acepté la escritura GATT". **No** significa que
  Xiaomi aceptó el comando de aplicación.

---

## 3. Framing SPP v2

```
A5 A5  <type>  <seq>  <len LE 2B>  <crc LE 2B>  <body…>
└──┬─┘    │      │        │             │
preamble  │      │        │             └─ CRC-16/ARC SOLO sobre el body
          │      │        └─ largo del body
          │      └─ número de secuencia (0-255)
          └─ tipo de frame (nibble bajo)
```

**CRC-16/ARC**: poly `0x8005` reflejado (`0xA001`), init `0x0000`, refin/refout true,
xorout `0x0000`, little-endian.

```python
def crc16_arc(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF
```

### Tipos de frame

| type | Nombre | Notas |
|------|--------|-------|
| `0x01` | **Ack** | body vacío → `a5a5 01 <seq> 0000 0000` |
| `0x02` | **SessionConfig** | handshake de sesión, TLVs |
| `0x03` | **Data** | comandos de aplicación |

La Band ACKea cada Data frame nuestro y espera que nosotros ACKeemos los suyos.

### Body de un Data frame

```
<channel> <opcode> <payload…>
```

| Constante | Valor |
|-----------|-------|
| `CHANNEL_PROTOBUF` | 1 (cifrado tras el auth) |
| `CHANNEL_DATA` | 2 (sin cifrar) |
| `CHANNEL_ACTIVITY` | 5 (cifrado) |
| `OPCODE_SEND_PLAINTEXT` | 1 |
| `OPCODE_SEND_ENCRYPTED` | 2 |

El auth usa **channel 1 + opcode 1**. Los comandos normales post-auth usan
**channel 1 + opcode 2**. El channel es el nibble bajo del primer byte.

---

## 4. Handshake de sesión

**No se puede mandar CMD_NONCE como primer paquete.** Primero hay que abrir la sesión.

### START_SESSION_REQUEST (TX)

```
a5a5020016001d4d  0101030001000002020000fc03020020000402001027
```

```
a5 a5          preamble
02             type = SessionConfig
00             seq = 0
16 00          len = 22
1d 4d          CRC-16/ARC del body = 0x4d1d   ✔ verificado
---- body ----
01             START_SESSION_REQUEST
01 03 00  01 00 00     TLV 1 version  = 1.0.0
02 02 00  00 fc        TLV 2 maxFrame = 0xfc00
03 02 00  20 00        TLV 3 txWindow = 32
04 02 00  10 27        TLV 4 timeout  = 10000 ms
```

TLVs: `<tag 1B> <len LE 2B> <value>`.

### START_SESSION_RESPONSE (RX)

```
a5a502001600232f  0201030003000902020000800302000300040200803e
```

```
02             START_SESSION_RESPONSE   (el body arranca en 02, no 01)
01 03 00  03 00 09     version  = 3.0.9
02 02 00  00 80        maxFrame = 32768
03 02 00  03 00        txWindow = 3
04 02 00  80 3e        timeout  = 16000 ms
```

---

## 5. Comandos

```
# Auth (COMMAND_TYPE = 1)
CMD_SEND_USERID = 5
CMD_NONCE       = 26   (0x1a)
CMD_AUTH        = 27   (0x1b)

# Health (COMMAND_TYPE = 8)
CMD_CONFIG_HEART_RATE_GET = 10
CMD_CONFIG_HEART_RATE_SET = 11
CMD_REALTIME_STATS_START  = 45
CMD_REALTIME_STATS_STOP   = 46
CMD_REALTIME_STATS_EVENT  = 47
```

El payload es un protobuf `Command { 1: type, 2: subtype, … }`.

### Campos protobuf de `Auth` (confirmados en el aire)

| field | tag | contenido |
|-------|-----|-----------|
| 30 | `f2 01` | `phoneNonce { 1: bytes(16) }` |
| 31 | `fa 01` | `watchNonce { 1: nonce(16), 2: hmac(32) }` |
| 32 | `82 02` | `authStep3 { 1: encryptedNonces(32), 2: encryptedDeviceInfo }` ← **TX** |
| 33 | `8a 02` | `authStep4 { 1: status, 2: … }` ← **RX, respuesta del auth** |

---

## 6. Autenticación

### Paso 1 — CMD_NONCE (TX)

```
Command { type: 1, subtype: 26, auth { phoneNonce { nonce: <16B random> } } }
```

Serializado: `08 01  10 1a  1a 15  f2 01 12  0a 10  <nonce16>` → body de 29 B (`0x1d`).

### Paso 2 — WatchNonce (RX)

La Band contesta un Ack y después un Data frame de 63 B:

```
01 01                                      channel=1, opcode=1
08 01  10 1a                               type=1, subtype=26
1a 37                                      field 3 = auth, len 55
   fa 01 34                                field 31 = watchNonce, len 52
      0a 10 <watch_nonce 16B>
      12 20 <watch_hmac 32B>
```

### Paso 3 — Derivación de claves

```python
hmac_key = HMAC_SHA256(key = phone_nonce + watch_nonce, msg = AUTH_KEY)

out, prev, ctr = b'', b'', 1
while len(out) < 64:
    prev = HMAC_SHA256(hmac_key, prev + b'miwear-auth' + bytes([ctr]))
    out += prev
    ctr += 1

decryption_key   = out[0:16]
encryption_key   = out[16:32]
decryption_nonce = out[32:36]   # sólo se usa en el CCM del auth
encryption_nonce = out[36:40]
```

**Oráculo de verificación** (offline, no requiere tocar la Band):

```python
assert HMAC_SHA256(decryption_key, watch_nonce + phone_nonce) == watch_hmac
```

Verificado con múltiples sesiones independientes, match exacto de los 32 bytes. Confirma
a la vez la derivación **y** que la auth key es correcta.

### Paso 4 — CMD_AUTH (TX)

```
Command { type: 1, subtype: 27, auth { authStep3 {
    1: encryptedNonces,
    2: encryptedDeviceInfo
} } }
```

**`encryptedNonces`** (el nombre engaña: es un HMAC, no un cifrado):

```python
encrypted_nonces = HMAC_SHA256(encryption_key, phone_nonce + watch_nonce)   # 32 B
```

**`encryptedDeviceInfo`** — acá estuvo el bloqueo: **va cifrado con AES-CCM**.

```python
device_info = protobuf AuthDeviceInfo {
    1: 0,              # unknown1
    2: 34,             # phoneApiLevel
    3: "SafePlaceHub", # phoneName
    4: 224,            # unknown3
    5: "ES",           # region
}

nonce  = encryption_nonce + b'\x00'*4 + (0).to_bytes(4, 'little')   # 12 B, contador 0
cipher = AES.new(encryption_key, AES.MODE_CCM, nonce=nonce, mac_len=4)
ct, mac = cipher.encrypt_and_digest(device_info)
encrypted_device_info = ct + mac
```

Nonce CCM: `encryption_nonce(4B) ‖ ceros(4B) ‖ contador(4B little-endian)`, tag de 4 bytes.

El contenido del `AuthDeviceInfo` es **irrelevante** para la Band (probado con campos
omitidos, valores distintos y mensaje vacío: todos aceptados). Lo que importa es que el
campo 2 **exista y esté cifrado**.

### Paso 5 — Respuesta de auth (RX)

```
0801101b  1a09  8a02 06  08 01  10 bcc211
type=1, sub=27, auth { authStep4 { 1: 1, 2: 287036 } }
```

**Criterio de éxito**: en Gadgetbridge es
`if (cmd.getSubtype() == CMD_AUTH || cmd.getAuth().getStatus() == 1)` — una respuesta con
subtype 27 **ya cuenta como éxito**, independientemente del status.

#### Códigos de status observados (campo 3 de `Auth`)

Útiles como diagnóstico mientras se depura:

| status | significado deducido |
|--------|---------------------|
| 7 | firma inválida — `encryptedNonces` mal calculado |
| 3 | firma OK pero `encryptedDeviceInfo` sin cifrar / mal formado |
| (sin campo 3, llega `authStep4`) | **éxito** |

Barrido de 8 variantes de clave para el proof: todas dieron 7 salvo
`HMAC(encryption_key, phone_nonce + watch_nonce)`, que dio 3. Eso aisló el problema al
device info.

Si el campo 2 falta por completo, la Band **no responde nada**.

---

## 7. Cifrado post-auth

**Ojo: NO es el mismo esquema que el del auth.** Post-auth se usa `encryptV2`/`decryptV2`,
que son **AES-CTR con la clave usada también como IV**, sin MAC y sin contador:

```python
def encrypt_v2(msg):
    return AES.new(encryption_key, AES.MODE_CTR,
                   nonce=b'', initial_value=encryption_key).encrypt(msg)

def decrypt_v2(ct):
    return AES.new(decryption_key, AES.MODE_CTR,
                   nonce=b'', initial_value=decryption_key).decrypt(ct)
```

(El comentario en el fuente de Gadgetbridge sobre esto es literalmente
"I wish I was kidding".)

Como el IV es fijo, el keystream se repite en cada mensaje. Se nota en el aire: frames con
el mismo plaintext salen byte a byte idénticos, y todos arrancan con el mismo byte. Eso fue
justamente la pista que delató que no era CCM.

No hay tag: **todo el payload es ciphertext**, no hay que recortar nada.

---

## 8. Lectura de heart rate

### Pedido

```
Command { type: 8, subtype: 45 }        # sin payload anidado
→ body = bytes([1, 2]) + encrypt_v2(b'\x08\x08\x10\x2d')
```

### Respuesta

```
pt: 0808 102d 520b ba0208 08001000180020 4e
    08 08        type = 8
    10 2d        subtype = 45
    52 0b        field 10 = health, len 11
       ba 02 08  field 39 = realTimeStats, len 8
          08 00     1: steps    = 0
          10 00     2: calories = 0
          18 00     3: ?        = 0
          20 4e     4: HEART RATE = 78 bpm
```

**El HR es `Command.field10.field39.field4`.** Gadgetbridge trata `heartRate <= 10` como
inválido.

La Band responde **una** muestra por pedido, con subtype 45 (el mismo del comando), no como
stream de eventos 47. Así que para el hub el patrón es **pollear**: mandar RT_START cada N
segundos y leer el valor.

### Validación

9 lecturas consecutivas cada 8 s, sesión única:

```
67, 78, 79, 81, 75, 77, 79, 79, 87 bpm
```

Variación natural en rango de reposo → es HR real, no un contador ni un valor fijo.

---

## 9. Flujo completo

```
1. bluetoothctl connect B8:53:84:31:A0:9C        (bonding ya hecho)
2. StartNotify en FE95/005E
3. TX START_SESSION_REQUEST          → RX START_SESSION_RESPONSE
4. TX CMD_NONCE  (ch1 op1)           → RX Ack + WatchNonce
5. derivar claves, verificar watch_hmac
6. TX CMD_AUTH   (ch1 op1)           → RX Ack + authStep4   [autenticado]
7. TX RT_START   (ch1 op2, AES-CTR)  → RX RealTimeStats con HR
8. repetir 7 cada N segundos
9. TX RT_STOP (type 8 sub 46) al cerrar
```

ACKear todo Data frame entrante con `a5a5 01 <seq> 0000 0000`.

---

## 10. Estado

- [x] Bonding
- [x] Transporte (`FE95` 005F TX / 005E RX)
- [x] Framing SPP v2 + CRC-16/ARC
- [x] START_SESSION
- [x] CMD_NONCE → WatchNonce
- [x] Derivación de claves verificada contra el hmac de la Band
- [x] CMD_AUTH (con deviceInfo cifrado en CCM)
- [x] Cifrado post-auth AES-CTR
- [x] **Lectura de HR real validada**
- [ ] Integrar como tipo de dispositivo en `ble_gateway.py`, al lado del Garmin
- [ ] Reconexión / reautenticación automática ante caída de la sesión

### Referencia

Implementación de referencia que llega a leer HR: `tools/xiaomi_band9.py`.

Captura HCI para depurar:
```bash
sudo setsid nohup btmon -i hci0 -w /tmp/cap.btsnoop < /dev/null > /dev/null 2>&1 &
# ... correr el test ...
sudo pkill -x btmon
sudo btmon -T -r /tmp/cap.btsnoop > /tmp/cap.txt
```

Cuidado: `pkill -f btmon` se mata a sí mismo por ssh (el patrón matchea la línea de comando
del propio shell). Usar siempre `pkill -x btmon`.

### Fuentes

El protocolo se reconstruyó cruzando captura en vivo con el código de Gadgetbridge:
`XiaomiAuthService.java`, `XiaomiSppPacketV2.java`, `services/XiaomiHealthService.java`.
