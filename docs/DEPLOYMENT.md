# การติดตั้ง

เอกสารนี้พาไปตั้งแต่เครื่องเปล่าจนพูดคุยได้ แบ่งเป็น 3 ส่วน: GPU node,
voice host, และการต่อทั้งสองเข้าด้วยกัน

## ภาพรวมเครื่องในระบบนี้

| บทบาท | เครื่อง | ที่อยู่ | หมายเหตุ |
|---|---|---|---|
| **GPU node** (ใช้งานจริง) | `rtx4000` | `100.113.214.111` (Tailscale) · `192.168.10.41` (LAN) | 2× RTX PRO 4000 Blackwell 24 GB · x86_64 |
| GPU node (ทางเลือก) | `dgx-msi-04` | `100.84.136.110` (Tailscale) | GB10 `aarch64` · GPU ถูกใช้อยู่แล้ว |
| **Voice host** | OrbStack VM `HermesJarvis` | `192.168.139.181` | Ubuntu `aarch64` ไม่มี GPU |
| Agent | เดียวกับ voice host | `127.0.0.1:8642` | Hermes Agent v0.20.5 |
| HUD | เดียวกับ voice host | `https://192.168.139.181:8766/hud/` | ต้องเป็น https |

**ทำไมเลือก `rtx4000` ไม่ใช่ `msi-4`** — GPU ของ msi-4 มี `llama-server` จองไว้
32.7 GB อยู่แล้ว ส่วน rtx4000 ว่างสนิท · และ rtx4000 เป็น x86_64 ซึ่งใช้ wheel
มาตรฐานได้ตรง ๆ ขณะที่ msi-4 เป็น GB10 `aarch64` ที่ต้องใช้ wheel คนละชุด

> ทั้งคู่เป็น **Blackwell** (sm_120 / sm_121) ซึ่งต้องใช้ torch `cu128` ขึ้นไป —
> `install.sh` เลือกให้เองจาก compute capability ไม่ต้องตั้งเอง

sidecar ไม่ผูกกับเครื่องใดเครื่องหนึ่ง — ย้าย node ได้โดยแก้ที่เดียวคือ
`stt.remote.url` กับ `voice.url` ใน `server.yaml` และจะแยก STT กับ TTS ไปคนละ
เครื่องก็ได้ถ้า VRAM ไม่พอ

---

## การเข้าถึง GPU node

สคริปต์ `deploy-gpu-node.sh` ต้องใช้ **SSH key auth** ไม่รองรับรหัสผ่าน
(โดยตั้งใจ — สคริปต์อัตโนมัติไม่ควรต้องมีคนพิมพ์รหัสผ่าน และรหัสผ่านใน
สคริปต์คือความเสี่ยง)

ถ้ายังเข้าไม่ได้ ให้ติดตั้ง public key ก่อนโดยรันจากเครื่องที่จะ deploy:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_jarvis_gpu -N "" -C "jarvis-deploy"
ssh-copy-id -i ~/.ssh/id_jarvis_gpu.pub neronain@100.113.214.111   # rtx4000
ssh-copy-id -i ~/.ssh/id_jarvis_gpu.pub neronain@100.84.136.110    # msi-4 (ถ้าจะใช้)
```

แล้วเพิ่มลง `~/.ssh/config`:

```
Host rtx4000
    HostName 100.113.214.111
    User neronain
    IdentityFile ~/.ssh/id_jarvis_gpu
    IdentitiesOnly yes

Host msi-4
    HostName 100.84.136.110
    User neronain
    IdentityFile ~/.ssh/id_jarvis_gpu
    IdentitiesOnly yes
```

ทดสอบ: `ssh rtx4000 nvidia-smi`

> **ถ้าใช้ LMDS อยู่แล้ว ไม่ต้องทำขั้นนี้** — LMDS ติดตั้ง key ของตัวเองไว้บน
> ทุกเครื่องในฟลีตแล้ว ใช้ `-i ~/.config/lmds/id_lmds` จาก hub ได้เลย

> `ssh-copy-id` ต้องพิมพ์รหัสผ่านครั้งเดียว หลังจากนั้นใช้ key ตลอด —
> `deploy-gpu-node.sh` ใช้ `BatchMode=yes` จึงไม่รับรหัสผ่านโดยเจตนา

---

## 1. GPU node

### ตรวจความพร้อม

```bash
ssh rtx4000
nvidia-smi                    # ต้องเห็น GPU และ driver version
python3 -V                    # ต้อง 3.10+
nvidia-smi --query-gpu=memory.total --format=csv
```

**VRAM ที่ต้องใช้:**

| โมเดล | float16 | int8_float16 |
|---|---|---|
| faster-whisper `large-v3` | ~4.7 GB | ~2.6 GB |
| F5-TTS-TH v2 | ~3.5 GB | — |
| **รวมเมื่อรันพร้อมกัน** | **~8.2 GB** | ~6.1 GB |

ถ้า VRAM น้อยกว่า 10 GB ให้ตั้ง `JARVIS_STT_COMPUTE=int8_float16`

### ติดตั้ง

```bash
# จากเครื่อง dev — เปลี่ยน msi-4 เป็น rtx4000 ได้ถ้าใช้เครื่องสำรอง
./scripts/deploy-gpu-node.sh rtx4000

# หรือบน node โดยตรง
git clone https://github.com/neronain/Jarvis-hermes.git
cd Jarvis-hermes/gpu-node
./install.sh --systemd
```

`install.sh` จะ:

1. ตรวจ Python และ GPU
2. สร้าง venv (`gpu-node/.venv`)
3. ติดตั้ง **torch แบบ CUDA ก่อน** — สำคัญ เพราะถ้าปล่อยให้ `f5-tts-th`
   ดึง torch เอง มันจะได้ CPU wheel แล้วทุกอย่างจะช้าโดยไม่มี error
4. ติดตั้ง dependency ที่เหลือจาก `requirements.txt`
5. ดาวน์โหลดน้ำหนักโมเดลล่วงหน้า (whisper `large-v3` ~3 GB, F5-TTS-TH ~1.4 GB)
6. ติดตั้ง systemd user units (เมื่อใส่ `--systemd`)

> ถ้า CUDA บน node ไม่ใช่ 12.4 ให้ระบุ index ให้ตรง:
> `JARVIS_TORCH_INDEX=https://download.pytorch.org/whl/cu121 ./install.sh`

### ตั้งค่า

```bash
cd ~/jarvis-gpu-node
cp .env.example .env
openssl rand -hex 32   # ใช้ค่านี้เป็น JARVIS_STT_TOKEN
openssl rand -hex 32   # และค่านี้เป็น JARVIS_TTS_TOKEN
$EDITOR .env
```

จากนั้นเตรียมเสียงอ้างอิง — ดู [THAI-TTS.md](THAI-TTS.md#การเตรียมเสียงอ้างอิง)

```bash
cp /path/to/clip.wav voices/jarvis_ref.wav
$EDITOR voices.yaml    # ใส่ ref_text ให้ตรงกับคลิปเป๊ะ ๆ
```

### เปิดบริการ

```bash
systemctl --user enable --now jarvis-stt jarvis-tts jarvis-stats
loginctl enable-linger "$USER"        # ไม่ให้บริการดับตอน logout
systemctl --user status jarvis-tts
```

การโหลดโมเดลครั้งแรกใช้เวลา 30–90 วินาที — `TimeoutStartSec=600` ใน unit
เผื่อไว้แล้ว ระหว่างนี้ `/health` จะตอบ `{"status":"loading"}`

### ตรวจ

```bash
curl -s localhost:8768/health | python3 -m json.tool
curl -s localhost:8769/health | python3 -m json.tool
```

---

## 2. Voice host

ติดตั้ง upstream `jarvis_ai` ตามเอกสารของมันก่อน แล้วค่อยวางส่วนของ repo นี้ทับ

```bash
git clone https://github.com/eadmin2/jarvis_ai.git
cd jarvis_ai
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # ตามที่ upstream กำหนด
```

### วาง adapter และ config

```bash
JH=/path/to/Jarvis-hermes
cp "$JH/host/adapters/f5_tts_provider.py" server/
cp "$JH/host/config/server.example.yaml" server/config/server.yaml
$EDITOR server/config/server.yaml       # แก้ IP ของ GPU node ถ้าไม่ใช่ 100.113.214.111
```

### ต่อ provider เข้ากับ voice server

upstream เรียก ElevenLabs ผ่าน provider เดียว หา call site แล้วสลับตาม
`voice.provider`:

```python
# server/server.py — ตรงที่เดิมสร้าง ElevenLabs provider
if cfg["voice"].get("provider") == "f5_tts_th":
    from f5_tts_provider import F5TTSProvider
    voice_provider = F5TTSProvider.from_config(cfg["voice"])
else:
    voice_provider = ElevenLabsProvider(...)   # ของเดิม
```

`F5TTSProvider.stream(text)` คืน iterator ของ PCM16 16 kHz — รูปแบบเดียวกับ
ที่ ElevenLabs provider คืน จึงต่อเข้ากับ path ส่งเสียงเดิมได้ทันที

### สร้าง TLS cert สำหรับ HUD

เบราว์เซอร์จะให้สิทธิ์ไมโครโฟนเฉพาะบน `https` (หรือ `localhost`) เท่านั้น
HUD บน LAN IP จึงใช้ไม่ได้ถ้าไม่มี cert:

```bash
./scripts/make-certs.sh ~/jarvis_ai
```

> **cert ต้องอยู่ที่ `<jarvis_ai>/server/certs/`** — `server.py` resolve
> `tls_cert` เทียบกับไดเรกทอรีของตัวเอง ไม่ใช่ root ของ repo · ถ้าวางไว้ที่
> `<jarvis_ai>/certs/` มันจะ **ข้าม TLS ไปเงียบ ๆ** ไม่มี error ให้เห็น
> พอร์ต 8766 จะไม่เปิดขึ้นมาเฉย ๆ

### ติดตั้งเป็น service

```bash
./scripts/install-host-service.sh ~/jarvis_ai
systemctl --user enable --now jarvis-voice
loginctl enable-linger "$USER"
```

### เปิด HUD ให้เข้าถึงได้

```yaml
# server.yaml — จำเป็น มิฉะนั้น socket จะหลุดตลอด
security:
  extra_origin_hosts: ["<LAN IP ของ host>", "<hostname>"]
server:
  local_name: "HERMES · ORB VM"     # ชื่อที่โชว์ในแผง MACHINES
```

แล้วต่อสาย panel MODELS LOADOUT ให้แสดงค่าจริง:

```bash
python host/patches/apply_hud_fixes.py ~/jarvis_ai
```

### ใส่ token

token อยู่ใน `~/.hermes/.env` (ไฟล์เดียวกับที่ Hermes ใช้):

```bash
JARVIS_STT_TOKEN=<ตรงกับบน GPU node>
JARVIS_TTS_TOKEN=<ตรงกับบน GPU node>
JARVIS_HUD_TOKEN=<token สำหรับเบราว์เซอร์>
```

---

## 3. ตรวจทั้งระบบ

```bash
cd /path/to/Jarvis-hermes
./scripts/healthcheck.sh 100.113.214.111
./scripts/smoke-test.sh  100.113.214.111
```

`smoke-test.sh` สังเคราะห์ประโยคไทย ส่งกลับไปให้ STT ถอด แล้วเทียบตัวอักษร
ที่ซ้อนทับกัน — ถ้าผ่านแปลว่าทั้งสอง sidecar และเส้นทางเครือข่ายใช้ได้จริง
ไม่ใช่แค่ `/health` ตอบ 200

## ลำดับการเปิดหลังรีบูต

GPU node ควรขึ้นก่อน host แต่ไม่บังคับ — ถ้า host ขึ้นก่อน STT จะตกไปใช้
local fallback ชั่วคราวและกลับมาใช้ GPU เองเมื่อ node พร้อม ส่วน TTS จะ
error จนกว่า node จะขึ้น (ไม่มี fallback เพราะไม่มีเสียงไทยบน CPU ที่ใช้ได้จริง)
