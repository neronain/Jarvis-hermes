# HUD — หน้าจอ J.A.R.V.I.S

หน้าเว็บที่ใช้คุยด้วยเสียง ดูว่า agent กำลังทำอะไร และอนุมัติคำสั่งที่เสี่ยง

## เข้าใช้งาน

```
https://192.168.139.181:8766/hud/
```

ใส่ HUD token เมื่อถูกถาม · หรือแปะครั้งเดียวแล้วมันเก็บเป็น cookie ให้:

```
https://192.168.139.181:8766/hud/?token=<TOKEN>
```

### ทำไมต้อง https

เบราว์เซอร์ให้สิทธิ์ไมโครโฟนเฉพาะบน **https หรือ localhost** เท่านั้น · เปิดผ่าน
`http://…:8765/hud/` หน้าจะขึ้นได้แต่กดพูดไม่ได้ · cert เป็น self-signed
เบราว์เซอร์จะเตือนครั้งเดียวต่อเครื่อง กด Advanced → Proceed แล้วจบ

สร้าง cert: `./scripts/make-certs.sh ~/jarvis_ai`
(**ต้องอยู่ที่ `server/certs/`** — ดู [DEPLOYMENT](DEPLOYMENT.md))

## Token

```bash
./scripts/new-hud-token.sh
```

สร้าง token ใหม่ เขียนลง `~/.hermes/.env` และ restart service ให้อัตโนมัติ

รูปแบบเป็น 3 กลุ่ม กลุ่มละ 5 ตัว เช่น `rnu37-6vx6w-4vv9d` ใช้ชุดตัวอักษรที่
**ตัด `0 O 1 l I` ออก** เพราะต้องพิมพ์บนคีย์บอร์ดมือถือ

**ทำไมไม่ใช้ token ยาว ๆ:** HUD เข้าถึงได้เฉพาะ LAN หรือ tailnet และการเดาแต่ละ
ครั้งต้องเปิด WebSocket handshake เต็มรูปแบบ — brute force ไม่ใช่ภัยที่แท้จริง
ส่วน token ที่ยาวจนอ่านไม่ไหวจะถูกก็อปวางในแชทแทนที่จะพิมพ์ ซึ่งเสี่ยงกว่า

> ใครถือ token นี้สั่ง agent ที่มีสิทธิ์รันคำสั่งบนเครื่องได้ — ปฏิบัติกับมัน
> เหมือนรหัสผ่าน และหมุนทันทีเมื่อหลุด

## Origin allowlist — ด่านที่คนติดกันมากที่สุด

WebSocket ของ HUD ต้องผ่าน **สองด่าน**: Origin host ต้องอยู่ใน allowlist
**และ** token ต้องถูก · ค่าเริ่มต้นมีแค่ `jarvis.local`, `jarvis`, `localhost`,
`127.0.0.1` — **เปิดด้วย LAN IP จะถูกปฏิเสธเสมอแม้ token ถูก**

```yaml
# server.yaml
security:
  extra_origin_hosts: ["192.168.139.181", "hermesjarvis.orb.local"]
```

อาการเมื่อลืม: มุมบนขึ้น `LINK DOWN`, Socket `offline`, ต่อใหม่ทุก 3 วินาที
โดยไม่มี error ให้เห็น

> `curl` จะ **ไม่มีวันเจอบั๊กนี้** เพราะไม่ส่ง `Origin` header ซึ่งโค้ดตีความว่า
> เป็น native client แล้วปล่อยผ่าน — ต้องทดสอบด้วยเบราว์เซอร์จริงเท่านั้น

## แผงต่าง ๆ

| แผง | แสดงอะไร | ที่มา |
|---|---|---|
| **VOICE LINK** | สถานะ socket / ไมค์ / ระดับเสียงเข้า | WebSocket |
| **AGENT ACTIVITY** | tool ที่ agent เรียก พร้อมตัวอย่างคำสั่ง | event `agent_status` |
| **TURN METRICS** | เวลาต่อเทิร์น แยก speech→audio และ total | `TurnTiming` |
| **MODELS LOADOUT** | brain / STT / TTS ที่ทำงานจริง | `/api/loadout` |
| **MACHINES** | CPU/RAM/GPU ของ host และ GPU node | `/api/machines` |
| **SKILLS** | skill ที่ Hermes โหลดไว้ | Hermes API |

### MODELS LOADOUT อ่านค่าจริง

ของ upstream แผงนี้เป็น **HTML ที่เขียนค่าตายตัว** (`whisper base.en`,
`ElevenLabs Flash v2.5`, `claude-haiku-4-5`) ไม่ได้อ่านจากระบบเลย · patch
`host/patches/apply_hud_fixes.py` เปลี่ยนให้ดึงจาก `/api/loadout` ซึ่ง:

- **Brain** — อ่าน `model.default` จาก `~/.hermes/config.yaml` ของ Hermes เอง
- **STT / TTS** — **ถาม sidecar สด ๆ** ทุก 30 วินาที ไม่ใช่อ่านจาก config

ข้อแตกต่างที่สำคัญ: ถ้า GPU node ดับ แผงจะขึ้น `offline` เป็นสีแดง แทนที่จะโชว์
ค่าที่ตั้งไว้ราวกับยังทำงานอยู่ · แผงสถานะที่โกหกแย่กว่าไม่มีแผงเลย

```bash
curl -sk --cookie "jarvis_token=$JARVIS_HUD_TOKEN" \
  https://192.168.139.181:8766/api/loadout
```

```json
{
  "brain": "vllm-spark-01/gemma4-26b-uncensored",
  "stt": "large-v3 · cuda · rtx4000",
  "tts": "F5-TTS-TH-v2 · cuda",
  "voice": "sample_th",
  "provider": "f5_tts_th"
}
```

แถวเดิม `Fallback` ถูกลบออก (ไม่ได้ตั้งไว้จริง) และ `11Labs quota` เปลี่ยนเป็น
`TTS chars today` พร้อมซ่อนแถบ quota เพราะเสียงรันเองไม่มีโควตา

### ชื่อเครื่องใน MACHINES

upstream เขียน `"MAC MINI · HERMES"` ตายตัวในโค้ด · ตั้งเองได้:

```yaml
server:
  local_name: "HERMES · ORB VM"
```

## API (ต้องมี auth)

HTTP API รับ token จาก **cookie หรือ header `X-Jarvis-Token`** เท่านั้น —
**ไม่รับ `?token=`** (ต่างจาก WebSocket ที่รับทั้งสองแบบ)

```bash
# แบบนี้ได้
curl -sk -H "X-Jarvis-Token: $TOKEN" https://host:8766/api/loadout
curl -sk --cookie "jarvis_token=$TOKEN" https://host:8766/api/loadout

# แบบนี้ได้ 401
curl -sk "https://host:8766/api/loadout?token=$TOKEN"
```

| Endpoint | คืนอะไร |
|---|---|
| `/api/loadout` | brain / STT / TTS ที่ทำงานจริง |
| `/api/machines` | สถิติเครื่อง host + GPU node |
| `/api/usage` | token ที่ใช้วันนี้ จำนวนเทิร์น |
| `/ws` | WebSocket ของเสียงและ event ทั้งหมด |

## VIEWS — Kanban / Hermes Dashboard

ปุ่มในแผง VIEWS เปิดหน้าเว็บของ Hermes ใน viewer ที่ซ้อนอยู่บน HUD

หน้าเหล่านี้ไม่ได้เสิร์ฟจาก voice server แต่มาจาก **dashboard ของ Hermes เอง**
ที่ผูกกับ `127.0.0.1:9119` แล้วให้ voice server ทำ TLS reverse proxy ที่ 9443:

```
เบราว์เซอร์ ──https :9443──▶ voice server (ตรวจ token) ──http :9119──▶ hermes dashboard
```

ที่ต้องมี proxy คั่นเพราะ dashboard ผูกกับ loopback อย่างเดียว และหน้า HUD เป็น
https จะ iframe หน้า http ไม่ได้ · proxy จึงทำสองอย่าง: ใส่ TLS และบังคับ auth

### เปิดใช้งาน

```bash
systemctl --user enable --now hermes-dashboard
```

ครั้งแรกต้อง build web UI ก่อน (ต้องมี npm):

```bash
hermes dashboard --port 9119 --host 127.0.0.1 --no-open   # build แล้ว Ctrl-C
```

> ใช้ `hermes serve` ไม่ได้ — เป็นโหมด headless ที่ปิด web UI ไว้ และจะตอบ
> `{"error":"Headless backend (hermes serve): web UI disabled"}` ต้องใช้
> `hermes dashboard` เท่านั้น

### ถ้ากดแล้วหน้าว่าง

เบราว์เซอร์จำ cert exception **แยกตามพอร์ต** · คุณกด Proceed ให้ `:8766` ไปแล้ว
แต่ `:9443` เป็นคนละ origin จึงยังไม่ได้รับอนุญาต และ iframe จะล้มแบบเงียบ ๆ

เปิด `https://<host>:9443/` ตรง ๆ หนึ่งครั้ง กด Advanced → Proceed แล้วกลับมา
ที่ HUD — ปุ่มจะทำงาน

> cookie ไม่ใช่ปัญหา: cookie ไม่แยกตามพอร์ต ตัวที่ตั้งไว้ตอนเข้า `:8766`
> ถูกส่งไป `:9443` ให้เองอยู่แล้ว

## ปุ่มลัด

| ปุ่ม | ทำอะไร |
|---|---|
| `Space` หรือคลิกวงแหวน | เริ่ม/หยุดพูด |
| `Esc` | หยุด agent กลางคัน (barge-in) |
| `B` | เล่นอนิเมชันบูต |

พิมพ์คุยก็ได้ผ่านช่องล่าง — ใช้ session เดียวกับเสียง ความจำจึงต่อเนื่องกัน
