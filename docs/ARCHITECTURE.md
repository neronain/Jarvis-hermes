# สถาปัตยกรรม

## หลักการออกแบบ

โปรเจกต์นี้ยึด 3 ข้อ:

1. **แยกงานตามฮาร์ดแวร์ที่เหมาะ** — งานที่ต้อง GPU อยู่บนเครื่องที่มี GPU
   งานที่ต้อง state (Hermes, memory, session) อยู่บนเครื่องที่เปิดตลอด
2. **ไม่แก้ upstream ถ้าไม่จำเป็น** — STT sidecar ใช้สัญญาเดียวกับ
   `jarvis_ai` เป๊ะ ๆ เพื่อให้ pull upstream มาแล้วไม่ต้อง merge ตลอด
3. **เสื่อมสภาพอย่างสง่างาม** — GPU node ดับ ระบบต้องยังพูดคุยได้ ไม่ใช่ตายทั้งระบบ

## ทำไมไม่รันทุกอย่างบนเครื่องเดียว

| ทางเลือก | ปัญหา |
|---|---|
| ทุกอย่างบน Mac / OrbStack | ไม่มี CUDA — OrbStack VM เป็น `aarch64` ไม่มี GPU passthrough `large-v3` บน CPU กินเวลาหลายวินาทีต่อประโยค และ F5-TTS ยิ่งช้ากว่า |
| ทุกอย่างบน GPU node | Hermes มี state (memory, sessions, cron, kanban) ที่ผูกกับเครื่องที่เปิดตลอด และ GPU node อาจถูกใช้งานอย่างอื่น/รีบูตบ่อย |
| แยกตามที่ทำอยู่ | latency ข้ามเครื่อง ~20 ms ซึ่งน้อยกว่า inference มาก และแต่ละฝั่งอัปเกรดแยกกันได้ |

**ตัวเลขที่วัดจริง:** OrbStack VM → GPU node ผ่าน Tailscale ได้ ~20 ms RTT
เทียบกับ STT ที่ 0.50 s และ TTS ที่ 0.59–1.02 s ต่อประโยค — network คิดเป็น
ไม่ถึง 2% ของเวลาทั้งหมด · ดูตัวเลขเต็มที่ [PERFORMANCE](PERFORMANCE.md)

## การไหลของหนึ่งเทิร์น

```
ผู้ใช้พูด
   │
   ├─ เบราว์เซอร์จับเสียง → PCM16 16 kHz → wss → voice host
   │
   ├─ voice host ส่ง PCM → GPU node :8768/stt
   │     ├─ สำเร็จ → ได้ข้อความไทย (~0.2 s)
   │     └─ ล้มเหลว/timeout → ถอดด้วย local CPU model แทน   ← ทางเสื่อมสภาพ
   │
   ├─ ข้อความ → Hermes Agent :8642 (session เดียวกับแชทพิมพ์)
   │     └─ agent เรียก tool, ค้นเว็บ, อ่านไฟล์, จำ ฯลฯ
   │
   ├─ คำตอบ (สตรีมมาทีละส่วน) → แบ่งเป็นประโยค
   │
   └─ แต่ละประโยค → GPU node :8769/tts → PCM16 → wss → ลำโพง
         └─ ประโยคแรกเริ่มเล่นขณะที่ประโยคหลังยังสังเคราะห์อยู่
```

### ทำไมแบ่งเป็นประโยค

F5-TTS สร้างเสียงทั้งก้อนก่อนคืนค่า (ไม่ streaming ในตัว) ถ้าส่งคำตอบยาว
ทั้งอันไป ผู้ใช้จะเงียบรอ 5–10 วินาที การแบ่งประโยคทำให้ได้ยินเสียงแรก
ภายใน ~1–2 วินาที และเวลาที่เหลือถูกซ่อนอยู่ใต้การเล่นเสียงประโยคก่อนหน้า

ตัวแบ่งประโยคถูกเขียนไว้สองที่ (`gpu-node/tts_server.py:split_sentences` และ
`host/adapters/f5_tts_provider.py:split_sentences`) โดยตั้งใจ — ฝั่ง host
แบ่งเองเพื่อไม่ต้องรอ round trip เพิ่ม ส่วนฝั่ง node เปิด `/split` ไว้ให้
client อื่นที่อยากได้ผลตรงกัน

## API reference

### STT sidecar — พอร์ต 8768

| Method | Path | คำอธิบาย |
|---|---|---|
| `GET` | `/health` | สถานะ, ชื่อโมเดล, เวลาถอดเฉลี่ย, สถิติสะสม |
| `POST` | `/stt` | body = PCM ดิบ int16 LE mono 16 kHz → `{"text","language","duration","latency"}` |

**Headers:** `X-Jarvis-Token` (บังคับเมื่อ `JARVIS_STT_TOKEN` ถูกตั้ง),
`X-Jarvis-Language` (override ภาษาต่อ request เช่น `auto`)

สัญญานี้เหมือน upstream ทุกประการ — ที่เพิ่มคือฟิลด์ `language`/`duration`/
`latency` ในคำตอบ ซึ่ง client เดิมที่อ่านแค่ `text` ไม่ได้รับผลกระทบ

### TTS sidecar — พอร์ต 8769

| Method | Path | คำอธิบาย |
|---|---|---|
| `GET` | `/health` | สถานะ, โมเดล, รายชื่อเสียง, สถิติ |
| `GET` | `/voices` | รายละเอียดเสียงที่นิยามไว้ |
| `POST` | `/tts` | JSON → audio bytes |
| `POST` | `/split` | `{"text"}` → `{"chunks":[...]}` ตัวแบ่งประโยคเดียวกับที่ server ใช้ |

**Response headers ของ `/tts`:**

| Header | ความหมาย |
|---|---|
| `X-Jarvis-Latency` | วินาทีที่ใช้สังเคราะห์ |
| `X-Jarvis-Audio-Seconds` | ความยาวเสียงที่ได้ |
| `X-Jarvis-Sample-Rate` | 24000 (`wav`) หรือ 16000 (`pcm16`) |
| `X-Jarvis-Voice` | ชื่อเสียงที่ใช้จริง |

เอา `X-Jarvis-Audio-Seconds` หารด้วย `X-Jarvis-Latency` จะได้ real-time
factor — ถ้าน้อยกว่า 1 แปลว่าสังเคราะห์ช้ากว่าเวลาเล่นจริง ซึ่งจะรู้สึกได้
ในบทสนทนา

### Stats — พอร์ต 8767

`GET /stats` → CPU, RAM, disk, GPU (ผ่าน `nvidia-smi`) และสถานะของ sidecar
ทั้งสองตัว ใช้กับ HUD panel `machines` ของ upstream

## ฝั่ง host

voice server ของ upstream (`jarvis_ai`) ทำหน้าที่: รับเสียงจากเบราว์เซอร์ผ่าน
WebSocket, เรียก STT, ส่งข้อความให้ Hermes Agent, แล้วส่งเสียงตอบกลับ

repo นี้แก้ upstream **2 จุด** ผ่าน patcher ที่รันซ้ำได้ ไม่ fork:

| patcher | เปลี่ยนอะไร | ทำไมไม่ fork |
|---|---|---|
| `apply_f5_tts.py` | ใส่ early return ใน `tts_chunks_sync` ให้ไปใช้ F5-TTS-TH | ทาง ElevenLabs เดิมยังอยู่ครบ ดึง upstream ใหม่ไม่ต้อง merge |
| `apply_hud_fixes.py` | เพิ่ม `/api/loadout` + ต่อสายแผง MODELS LOADOUT | แผงเดิมเป็น HTML ตายตัว |

### `/api/loadout`

endpoint ที่เพิ่มเข้าไป ตอบว่า "ตอนนี้อะไรทำงานอยู่จริง" โดย:

- **brain** อ่าน `model.default` จาก `~/.hermes/config.yaml`
- **stt / tts** ยิง `/health` ไปถาม sidecar สด ๆ (cache 30 วินาที)

ที่ยอมจ่ายค่า network call แทนการอ่าน config เพราะ config บอกแค่ว่า *ตั้งใจ*
ให้อะไรทำงาน ไม่ได้บอกว่ามันทำงานอยู่จริง — sidecar ที่ดับต้องขึ้นว่าดับ

## การตัดสินใจที่ควรรู้

### ทำไมล็อกการ inference

ทั้งสอง sidecar ใช้ `threading.Lock` ครอบการ inference — GPU มีตัวเดียว
การปล่อยให้ request ชนกันทำให้ VRAM แตกและ latency แกว่งกว่าการเข้าคิว
ถ้าต้องรองรับผู้ใช้พร้อมกันหลายคน ทางที่ถูกคือเพิ่ม replica ไม่ใช่ถอดล็อก

### ทำไม resample ด้วย linear interpolation

`_resample()` ใน `tts_server.py` ใช้ `np.interp` ตรง ๆ ไม่ใช้ filter
สำหรับเสียงพูด 24 k → 16 k ความเพี้ยนที่เกิดอยู่เหนือย่านที่หูจับได้ในบริบท
โทรศัพท์/ลำโพง และการเลี่ยง `scipy`/`librosa` ทำให้ dependency บาง — ถ้า
ต้องการคุณภาพสูงกว่านี้ ให้ตั้ง `JARVIS_TTS_PCM_RATE=24000` แล้วให้ฝั่ง host
resample เอง

### ทำไมประโยคที่ล้มเหลวถูกข้าม ไม่ใช่โยน error

ใน `F5TTSProvider.stream()` ประโยคที่สังเคราะห์ไม่สำเร็จจะถูก log แล้วข้าม
เพราะเสียหนึ่งประโยคดีกว่าเสียทั้งเทิร์น ผู้ใช้จะได้ยินคำตอบที่ขาดไปหนึ่ง
ประโยค แทนที่จะเงียบสนิทแล้วไม่รู้ว่าเกิดอะไรขึ้น

### ทำไม HUD ต้องผ่านสองด่าน

`_ws_allowed` ตรวจทั้ง Origin host และ token · Origin กัน CSRF-style attack
จากหน้าเว็บอื่นที่เปิดในเบราว์เซอร์เดียวกัน ส่วน token กันคนอื่นในเครือข่าย
เดียวกัน — คนละภัย จึงต้องมีทั้งคู่

ผลข้างเคียงที่ต้องรู้: client ที่ไม่ส่ง `Origin` (สคริปต์ Python, `curl`,
push-to-talk client) ถูกตีความว่าเป็น native client และ **ข้ามด่าน Origin ไปเลย**
ซึ่งแปลว่าการทดสอบด้วย `curl` จะไม่มีวันเจอปัญหา Origin

### ทำไม token คนละตัวสำหรับ STT และ TTS

แยกไว้เพื่อให้เพิกถอนทีละส่วนได้ และเพราะ TTS มีต้นทุน GPU สูงกว่ามาก —
ถ้า token หลุด การจำกัดความเสียหายไว้ที่ตัวเดียวย่อมดีกว่า
