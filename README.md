# Jarvis-hermes

> ผู้ช่วยเสียงภาษาไทยแบบ J.A.R.V.I.S. สำหรับ [Hermes Agent](https://github.com/NousResearch/hermes-agent) — หู ปาก และสมองแยกกันคนละเครื่อง ต่อกันด้วย HTTP บน Tailscale

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![CUDA](https://img.shields.io/badge/GPU-NVIDIA%20CUDA-76B900)](https://developer.nvidia.com/cuda-toolkit)

Jarvis-hermes ต่อยอดจาก [`eadmin2/jarvis_ai`](https://github.com/eadmin2/jarvis_ai)
(HUD + voice pipeline สำหรับ Hermes Agent) โดยเปลี่ยน 2 อย่างให้เป็นภาษาไทยและ
รันบน GPU ของตัวเอง:

| | upstream `jarvis_ai` | Jarvis-hermes |
|---|---|---|
| **หู (STT)** | faster-whisper `small.en` บน CPU ของ host | **`large-v3` ภาษาไทย บน GPU node** |
| **ปาก (TTS)** | ElevenLabs (cloud, มีค่าใช้จ่าย, ไม่มีเสียงไทยที่ดี) | **F5-TTS-TH บน GPU node (local, ฟรี, clone เสียงได้)** |
| **สมอง** | Hermes Agent | Hermes Agent (ไม่เปลี่ยน) |
| **ข้อมูลออกนอกเครื่อง** | เสียงทุกประโยคส่งไป ElevenLabs | ไม่มีเสียงออกนอก tailnet |

---

## ทำไมต้องแยกเครื่อง

คำถามตั้งต้นของโปรเจกต์นี้คือ *"Hermes รันบน OrbStack บน Mac ซึ่งไม่มี CUDA
แล้วจะเอาโมเดลไปลงเครื่องอื่นแล้วเรียกใช้ได้ไหม"*

**ได้ และเป็นวิธีที่ถูกต้องอยู่แล้ว** — เพราะ:

1. **Mac ไม่มี CUDA จริง** OrbStack VM เป็น `aarch64` Linux ที่ไม่มี GPU
   passthrough ต่อให้ลง `faster-whisper` ได้ก็ตกไปใช้ CPU ซึ่งช้าเกินกว่าจะ
   ใช้เป็นผู้ช่วยเสียง และ F5-TTS บน CPU ยิ่งไม่ไหว
2. **`jarvis_ai` ออกแบบมารองรับอยู่แล้ว** upstream มี `stt.remote` config
   สำหรับ GPU sidecar อยู่ในตัว — ฝั่ง STT เราจึงแทบไม่ต้องเขียนใหม่
3. **Tailscale ทำให้ latency ไม่ใช่ปัญหา** วัดจริงจาก Mac ไป `dgx-msi-04` ได้
   ~20 ms ซึ่งน้อยกว่าเวลา inference หลายเท่า

ส่วนที่ upstream **ไม่มี** คือ TTS ภาษาไทย — นั่นคืองานหลักของ repo นี้

---

## สถาปัตยกรรม

```
   เบราว์เซอร์ / มือถือ (HUD)
            │ wss
            ▼
┌───────────────────────────────┐        ┌──────────────────────────────┐
│  Voice host                   │        │  GPU node — msi-4            │
│  OrbStack VM "HermesJarvis"   │        │  100.84.136.110 (Tailscale)  │
│  aarch64 · ไม่มี GPU           │        │  NVIDIA CUDA                 │
│                               │        │                              │
│  ┌─────────────────────────┐  │  HTTP  │  ┌────────────────────────┐  │
│  │ voice pipeline server   │──┼───────►│  │ STT :8768              │  │
│  │  · HUD + auth + TLS     │  │  PCM   │  │  faster-whisper        │  │
│  │  · STT fallback (CPU)   │◄─┼────────┼──│  large-v3 · ไทย         │  │
│  │  · F5 TTS adapter       │  │  text  │  └────────────────────────┘  │
│  └───────────┬─────────────┘  │        │  ┌────────────────────────┐  │
│              │                │  HTTP  │  │ TTS :8769              │  │
│              │                ├───────►│  │  F5-TTS-TH v2          │  │
│              │                │  text  │  │  zero-shot voice clone │  │
│              │                │◄───────┼──│  → PCM16 16 kHz        │  │
│              │                │  audio │  └────────────────────────┘  │
│              │                │        │  ┌────────────────────────┐  │
│              │                │◄───────┼──│ stats :8767            │  │
│  ┌───────────▼─────────────┐  │        │  └────────────────────────┘  │
│  │ Hermes Agent  :8642     │  │        └──────────────────────────────┘
│  │  memory · tools · skills│  │
│  └───────────┬─────────────┘  │
└──────────────┼────────────────┘
               │ OpenAI-compatible
               ▼
        LLM gateway (Bifrost)
```

รายละเอียดการไหลของข้อมูลและเหตุผลของแต่ละการตัดสินใจ ดู
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

---

## เริ่มใช้งาน

### สิ่งที่ต้องมี

| | |
|---|---|
| **GPU node** | Linux + NVIDIA GPU (VRAM ≥ 12 GB สำหรับ `large-v3` + F5-TTS พร้อมกัน), Python 3.10+ |
| **Voice host** | เครื่องที่รัน Hermes Agent อยู่แล้ว, Python 3.11+ |
| **เครือข่าย** | host เห็น GPU node ได้ (โปรเจกต์นี้ใช้ Tailscale) |
| **เสียงอ้างอิง** | คลิปเสียงไทย 5–15 วินาที + ข้อความถอดเสียงที่ตรงกันเป๊ะ |

### 1. ติดตั้งบน GPU node

```bash
git clone https://github.com/neronain/Jarvis-hermes.git
cd Jarvis-hermes
./scripts/deploy-gpu-node.sh neronain@100.84.136.110
```

หรือถ้าอยู่บนเครื่อง GPU อยู่แล้ว:

```bash
cd gpu-node
./install.sh --systemd
cp .env.example .env      # ใส่ token ทั้งสองตัว
# วางคลิปเสียงอ้างอิงใน voices/ แล้วแก้ voices.yaml
systemctl --user enable --now jarvis-stt jarvis-tts jarvis-stats
loginctl enable-linger "$USER"
```

### 2. ตั้งค่าฝั่ง host

```bash
cp host/config/server.example.yaml <jarvis_ai>/server/config/server.yaml
cp host/adapters/f5_tts_provider.py <jarvis_ai>/server/
```

ใส่ token ลงใน `~/.hermes/.env` (ต้องตรงกับฝั่ง GPU node):

```bash
JARVIS_STT_TOKEN=<ค่าเดียวกับบน node>
JARVIS_TTS_TOKEN=<ค่าเดียวกับบน node>
JARVIS_HUD_TOKEN=<token สำหรับเปิด HUD บนเบราว์เซอร์>
```

ขั้นตอนเต็มพร้อมการต่อเข้ากับ `jarvis_ai` ดู
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)

### 3. ตรวจว่าใช้งานได้

```bash
./scripts/healthcheck.sh              # ทุกบริการขึ้นครบไหม
./scripts/smoke-test.sh               # พูด → ถอดกลับ → เทียบข้อความ
```

---

## API ของ GPU node

### STT — `POST :8768/stt`

รับ PCM ดิบ (int16 little-endian, mono, 16 kHz) — สัญญาเดียวกับ upstream
`jarvis_ai` ทุกประการ จึงใช้กับ voice server เดิมได้โดยไม่ต้องแก้

```bash
curl -X POST http://100.84.136.110:8768/stt \
  -H "X-Jarvis-Token: $JARVIS_STT_TOKEN" \
  --data-binary @speech.pcm
# {"text":"สวัสดีครับ","language":"th","duration":1.9,"latency":0.21}
```

### TTS — `POST :8769/tts`

```bash
curl -X POST http://100.84.136.110:8769/tts \
  -H "X-Jarvis-Token: $JARVIS_TTS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"สวัสดีครับ ระบบพร้อมทำงาน","format":"wav"}' \
  --output out.wav
```

| ฟิลด์ | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `text` | *(บังคับ)* | ข้อความที่จะอ่าน |
| `voice` | `default` ใน `voices.yaml` | ชื่อเสียงที่นิยามไว้ |
| `format` | `wav` | `wav` = 24 kHz, `pcm16` = raw 16 kHz สำหรับ pipeline |
| `speed` | ตามเสียงนั้น | 0.8–1.3 อยู่ในช่วงที่ยังฟังเป็นธรรมชาติ |
| `step` | `32` | ลดลงเพื่อความเร็ว แลกกับความคมของเสียง |
| `cfg` | `2.0` | ยิ่งสูงยิ่งเกาะเสียงอ้างอิง แต่เสี่ยงเสียงแข็ง |

รายการ endpoint ครบ ดู [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#api-reference)

---

## เอกสาร

| เอกสาร | เนื้อหา |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | การไหลของข้อมูล, สัญญา API, เหตุผลการออกแบบ |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | ติดตั้งจากศูนย์ทั้งสองฝั่ง + ต่อกับ `jarvis_ai` |
| [THAI-TTS](docs/THAI-TTS.md) | F5-TTS-TH, การทำเสียงอ้างอิง, การจูนคุณภาพ |
| [CONFIGURATION](docs/CONFIGURATION.md) | ตัวแปรทุกตัวและผลของมัน |
| [OPERATIONS](docs/OPERATIONS.md) | runbook: ดู log, restart, อัปเดต, สำรอง |
| [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) | อาการ → สาเหตุ → วิธีแก้ |
| [SECURITY](docs/SECURITY.md) | โมเดลภัยคุกคาม, การจัดการ token, การเปิดพอร์ต |

---

## สถานะโปรเจกต์

| ส่วน | สถานะ |
|---|---|
| STT sidecar (`large-v3`, ไทย) | ✅ เขียนเสร็จ — รอทดสอบบน msi-4 |
| Thai TTS sidecar (F5-TTS-TH) | ✅ เขียนเสร็จ — รอทดสอบบน msi-4 |
| Host adapter (แทน ElevenLabs) | ✅ เขียนเสร็จ — รอต่อเข้า `jarvis_ai` |
| systemd units + installer | ✅ เขียนเสร็จ |
| ทดสอบบน GPU จริง | ⏳ ยังไม่ได้รัน — ต้องมี SSH เข้า msi-4 ก่อน |
| Streaming TTS แบบคำต่อคำ | 📋 ยังไม่ทำ (ตอนนี้แบ่งเป็นประโยค) |
| Wake word ("จาร์วิส") | 📋 ยังไม่ทำ |

> **หมายเหตุ:** โค้ดทั้งหมดผ่านการตรวจ syntax แล้ว แต่ยัง**ไม่ได้รันบน GPU
> จริง** เพราะยังเข้า msi-4 ด้วย SSH key ไม่ได้ ดู
> [DEPLOYMENT § การเข้าถึง GPU node](docs/DEPLOYMENT.md#การเข้าถึง-gpu-node)

---

## เครดิต

- [`eadmin2/jarvis_ai`](https://github.com/eadmin2/jarvis_ai) — HUD, voice pipeline และแนวคิด GPU sidecar (MIT)
- [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) — ตัว agent
- [`SYSTRAN/faster-whisper`](https://github.com/SYSTRAN/faster-whisper) — STT
- [F5-TTS](https://arxiv.org/abs/2410.06885) และ [`VIZINTZOR/F5-TTS-TH-V2`](https://huggingface.co/VIZINTZOR/F5-TTS-TH-V2) — TTS ภาษาไทย

## สัญญาอนุญาต

[MIT](LICENSE) — เช่นเดียวกับ upstream ดู [NOTICE](NOTICE) สำหรับที่มาของโค้ด
