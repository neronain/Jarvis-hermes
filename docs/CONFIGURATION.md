# การตั้งค่า

การตั้งค่าอยู่ 3 ที่ แยกตามอายุการใช้งานและความลับ:

| ที่ | เก็บอะไร | commit ได้ไหม |
|---|---|---|
| `gpu-node/.env` | ความลับและพารามิเตอร์ของ node | ❌ |
| `gpu-node/voices.yaml` | นิยามเสียง | ✅ (แต่ไฟล์ `.wav` ไม่) |
| `<jarvis_ai>/server/config/server.yaml` | การตั้งค่า pipeline | ✅ |
| `~/.hermes/.env` | ความลับฝั่ง host | ❌ |

---

## ตัวแปรของ GPU node

### ร่วม

| ตัวแปร | ค่าเริ่มต้น | ผล |
|---|---|---|
| `JARVIS_VENV` | `<gpu-node>/.venv` | ตำแหน่ง venv ที่สคริปต์ใช้ |

### STT

| ตัวแปร | ค่าเริ่มต้น | ผล |
|---|---|---|
| `JARVIS_STT_TOKEN` | *(ว่าง)* | shared secret — **ว่าง = ไม่ตรวจสิทธิ์เลย** |
| `JARVIS_STT_MODEL` | `large-v3` | `large-v3-turbo` เร็วกว่า ~2 เท่า แม่นน้อยกว่าเล็กน้อยกับภาษาไทย |
| `JARVIS_STT_PORT` | `8768` | |
| `JARVIS_STT_HOST` | `0.0.0.0` | ตั้งเป็น tailnet IP เพื่อจำกัดการเข้าถึง |
| `JARVIS_STT_LANGUAGE` | `th` | `auto` สำหรับพูดไทยปนอังกฤษ — แลกกับความแม่นในคลิปสั้น |
| `JARVIS_STT_COMPUTE` | `float16` | `int8_float16` ลด VRAM ~45% แลกความแม่นเล็กน้อย |
| `JARVIS_STT_BEAM` | `5` | `1` เร็วขึ้นชัดเจน แม่นลดลงเล็กน้อย |
| `JARVIS_STT_VAD` | `1` | ตัดความเงียบก่อนถอด — ลดการ hallucinate ในคลิปที่มีแต่เสียงรบกวน |
| `JARVIS_STT_WARMUP` | `1` | โหลด kernel ตอนบูตแทนตอน request แรก |

### TTS

| ตัวแปร | ค่าเริ่มต้น | ผล |
|---|---|---|
| `JARVIS_TTS_TOKEN` | *(ว่าง)* | shared secret — **ว่าง = ไม่ตรวจสิทธิ์เลย** |
| `JARVIS_TTS_MODEL` | `v2` | `v1` = ไทยเป็นธรรมชาติกว่า, `v2` = อ่านผิดน้อยกว่า |
| `JARVIS_TTS_PORT` | `8769` | |
| `JARVIS_TTS_HOST` | `0.0.0.0` | |
| `JARVIS_TTS_VOICES` | `<gpu-node>/voices.yaml` | |
| `JARVIS_TTS_PCM_RATE` | `16000` | **ต้องตรงกับ `voice.sample_rate` ฝั่ง host** |
| `JARVIS_TTS_STEP` | `32` | ดู [THAI-TTS](THAI-TTS.md#การจูนคุณภาพกับความเร็ว) |
| `JARVIS_TTS_CFG` | `2.0` | |
| `JARVIS_TTS_SPEED` | `1.0` | ค่ากลาง ถูก override ด้วยค่าในแต่ละเสียง |
| `JARVIS_TTS_WARMUP` | `1` | |

### Stats

| ตัวแปร | ค่าเริ่มต้น |
|---|---|
| `JARVIS_STATS_PORT` | `8767` |
| `JARVIS_STATS_HOST` | `0.0.0.0` |

---

## `voices.yaml`

```yaml
default: jarvis        # ใช้เมื่อ request ไม่ระบุ voice

voices:
  jarvis:
    description: "คำอธิบาย — โผล่ใน /voices"
    ref_audio: voices/jarvis_ref.wav    # relative = เทียบกับที่อยู่ของ voices.yaml
    ref_text: "ข้อความที่ตรงกับคลิปเป๊ะ ๆ"
    speed: 1.0
```

server ตรวจตอนบูตว่าไฟล์เสียงมีจริงและ `ref_text` ไม่ว่าง ถ้าผิดจะ **ไม่ยอม
start** พร้อมบอกว่าเสียงไหนผิด — ตั้งใจให้ fail ตอนบูตดีกว่าไปพังตอนผู้ใช้พูด

---

## `server.yaml` — ส่วนที่ต่างจาก upstream

### `stt.remote`

```yaml
stt:
  model: small              # fallback บน CPU ของ host
  language: th
  remote:
    name: msi-4
    url: http://100.84.136.110:8768/stt
    token_env: JARVIS_STT_TOKEN
    timeout: 6              # เกินนี้ตกไปใช้ local
```

`timeout: 6` เผื่อไว้สำหรับประโยคยาว การตั้งต่ำเกินไปทำให้ระบบสลับไปใช้
โมเดล CPU บ่อยโดยไม่จำเป็น ซึ่งผู้ใช้จะรู้สึกได้จากความแม่นที่ตกลง

### `voice`

```yaml
voice:
  provider: f5_tts_th
  url: http://100.84.136.110:8769
  voice_name: jarvis        # ต้องมีใน voices.yaml ของ node
  token_env: JARVIS_TTS_TOKEN
  speed: 1.0
  step: 32
  cfg: 2.0
  timeout: 30               # ต่อ 1 ประโยค ไม่ใช่ทั้งคำตอบ
  sample_rate: 16000        # ต้องตรงกับ JARVIS_TTS_PCM_RATE
```

### `hermes.instructions`

ส่วนนี้มีผลต่อคุณภาพเสียงมากกว่าที่คิด เพราะกำหนดว่า agent จะเขียนอะไรมาให้
TTS อ่าน คำสั่งที่ควรมี:

- ห้าม markdown / bullet / code block — TTS จะอ่านสัญลักษณ์ออกมาตรง ๆ
- ตอบสั้น 1–3 ประโยค — ยาวกว่านี้ผู้ใช้รอนาน
- อ่านตัวเลข/วันที่/เวลาเป็นคำพูด — แก้ที่ต้นทางดีกว่าไปแก้ที่ TTS
- ห้ามพูดความลับออกเสียง

---

## ค่าที่ต้องตรงกันทั้งสองฝั่ง

ผิดคู่ไหนคู่หนึ่งแล้วอาการจะกำกวม ตรวจก่อนไล่หาที่อื่น:

| GPU node | Host | อาการเมื่อไม่ตรง |
|---|---|---|
| `JARVIS_STT_TOKEN` | `JARVIS_STT_TOKEN` | STT ตอบ 401 → ตกไปใช้ CPU เงียบ ๆ ความแม่นตก |
| `JARVIS_TTS_TOKEN` | `JARVIS_TTS_TOKEN` | ไม่มีเสียงออก มี error ใน log ฝั่ง host |
| `JARVIS_TTS_PCM_RATE` | `voice.sample_rate` | เสียงพูดเร็วหรือช้าผิดปกติ เหมือนเทปยืด |
| ชื่อเสียงใน `voices.yaml` | `voice.voice_name` | TTS ตอบ 404 พร้อมรายชื่อที่มี |
