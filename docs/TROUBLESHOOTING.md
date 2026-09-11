# แก้ปัญหา

เรียงจากอาการที่พบบ่อยที่สุด แต่ละหัวข้อไล่จาก **อาการ → สาเหตุที่เป็นไปได้ →
วิธียืนยัน → วิธีแก้**

---

## ทุกอย่างช้ามาก — ทั้งที่มี GPU

**สาเหตุที่พบบ่อยที่สุดคือ torch ที่ติดตั้งเป็น CPU build** ซึ่งจะไม่ error
เลย แค่ช้าลง 20–50 เท่า

```bash
ssh rtx4000
cd ~/jarvis-gpu-node && source .venv/bin/activate
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

ถ้าได้ `False` หรือเวอร์ชันลงท้ายด้วย `+cpu`:

```bash
pip uninstall -y torch torchaudio
pip install --index-url https://download.pytorch.org/whl/cu124 torch torchaudio
systemctl --user restart jarvis-stt jarvis-tts
```

> เกิดจากการที่ `f5-tts-th` ดึง torch เองเมื่อยังไม่มี — `install.sh` ลง CUDA
> build ก่อนเพื่อกันเรื่องนี้ แต่ถ้าเคยลงมือเองมาก่อนอาจติดค้าง

---

## TTS ไม่ยอม start

### `voice 'jarvis': ref_audio not found`

path ใน `voices.yaml` ผิด หรือยังไม่ได้วางไฟล์คลิป

```bash
ssh rtx4000 'ls -la ~/jarvis-gpu-node/voices/'
```

path แบบ relative จะถูกตีความเทียบกับที่อยู่ของ `voices.yaml` ไม่ใช่ cwd

### `voice 'jarvis': ref_text is required`

ต้องใส่ข้อความถอดเสียงของคลิป ปล่อยว่างไม่ได้ —
ดู [THAI-TTS](THAI-TTS.md#เขียน-ref_text-ให้ตรง)

### `CUDA out of memory`

STT กับ TTS แย่ง VRAM กัน

```bash
nvidia-smi
```

แก้โดยลด VRAM ของ STT:

```bash
# ใน .env บน node
JARVIS_STT_COMPUTE=int8_float16
```

หรือแยกไปคนละเครื่อง — sidecar สองตัวไม่จำเป็นต้องอยู่ node เดียวกัน

---

## HUD ขึ้น "LINK DOWN" / Socket offline

WebSocket ของ HUD ต้องผ่าน **สองด่าน** ไม่ใช่ด่านเดียว:

```python
if host not in ALLOWED_ORIGIN_HOSTS: return False   # ด่าน 1
return ws.cookies.get("jarvis_token") == token      # ด่าน 2
```

`ALLOWED_ORIGIN_HOSTS` มีแค่ `jarvis.local`, `jarvis`, `localhost`, `127.0.0.1`
**เปิด HUD ด้วย LAN IP จึงถูกปฏิเสธเสมอ** แม้ token ถูกต้อง และอาการที่เห็นคือ
socket ต่อแล้วหลุดทุก 3 วินาที ไม่มี error ให้เห็นบนหน้าจอ

```yaml
# server.yaml
security:
  extra_origin_hosts: ["192.168.139.181", "hermesjarvis.orb.local"]
```

> ทดสอบด้วย `curl`/สคริปต์จะ **ไม่เจอปัญหานี้** เพราะไม่ส่ง `Origin` header
> ซึ่งโค้ดตีความว่าเป็น native client แล้วปล่อยผ่าน — ต้องทดสอบด้วยเบราว์เซอร์จริง

---

## API ตอบ 401 ทั้งที่ใส่ `?token=`

WebSocket รับ token ได้ทั้งจาก cookie และ query param แต่ **HTTP API รับเฉพาะ
cookie หรือ header `X-Jarvis-Token`**:

```bash
curl -sk --cookie "jarvis_token=$JARVIS_HUD_TOKEN" https://host:8766/api/loadout
```

บนเบราว์เซอร์ไม่ต้องทำอะไร — HUD เก็บ cookie ให้เองหลังใส่ token ครั้งแรก

---

## MODELS LOADOUT แสดงค่าผิด

ของ upstream เป็น **HTML ที่เขียนค่าตายตัว** ไม่ได้อ่านจากระบบเลย · patch
`host/patches/apply_hud_fixes.py` เปลี่ยนให้ดึงจาก `/api/loadout` ซึ่งอ่าน
model จริงของ Hermes และถาม sidecar ทั้งสองตัวสด ๆ

```bash
python host/patches/apply_hud_fixes.py ~/jarvis_ai
systemctl --user restart jarvis-voice
```

---

## TTS ตอบ 500 — `TorchCodec is required for load_with_torchcodec`

torchaudio ตั้งแต่ 2.9 เลิกมี decoder ของตัวเองและไปเรียก `torchcodec` ซึ่ง
ต้องมี FFmpeg ระดับระบบ · `tts_server.py` ตรวจเรื่องนี้ตอนบูตและสลับไปใช้
`soundfile` แทนอัตโนมัติ ถ้ายังเจอ error นี้แปลว่ารันโค้ดเวอร์ชันเก่าอยู่:

```bash
ssh rtx4000 'cd ~/Jarvis-hermes && git pull && systemctl --user restart jarvis-tts'
journalctl --user -u jarvis-tts | grep "backed by soundfile"
```

ผลข้างเคียงที่ต้องรู้: **คลิปอ้างอิงต้องเป็น WAV** เพราะ `soundfile` ไม่อ่าน
mp3/m4a และ pydub ก็ต้องพึ่ง FFmpeg สำหรับฟอร์แมตเหล่านั้น

---

## systemd อ่านค่า env ผิด — `Invalid compute type: float16    # ...`

`EnvironmentFile=` ของ systemd **ไม่ตัด comment ท้ายบรรทัด** — `VAR=value  # note`
จะได้ค่าเป็น `value  # note` ทั้งก้อน (ต่างจาก `source` ของ bash ที่ตัดให้)

```bash
grep -nE '^[A-Z_]+=[^#]*[^ ]+[[:space:]]+#' ~/Jarvis-hermes/gpu-node/.env
sed -i -E 's/^([A-Z_]+=[^#]*[^ #])[[:space:]]+#.*$/\1/' ~/Jarvis-hermes/gpu-node/.env
systemctl --user restart jarvis-stt jarvis-tts
```

---

## เสียงพูดติด ๆ หยุดเป็นช่วง ๆ

F5-TTS ใส่ความเงียบหัวท้ายให้ทุกประโยค (วัดได้ 0.16–0.50 วิ หน้า, 0.04–0.32 วิ
หลัง) · pipeline สังเคราะห์ทีละประโยค พอต่อกันความเงียบจึงซ้อนกันเป็น **~0.6
วินาทีทุกรอยต่อ**

`tts_server.py` ตัดให้อัตโนมัติแล้ว ถ้ายังเจอ:

```bash
curl -s http://<node>:8769/health | grep -o '"trim_silence":[a-z]*'
```

ต้องได้ `true` · ถ้าเป็น `false` ให้ตั้ง `JARVIS_TTS_TRIM=1` แล้ว restart

ปรับจังหวะเว้นระหว่างประโยคได้ที่ `voice.pause_ms` ใน `server.yaml`
(ค่าเริ่มต้น 140 ms — น้อยกว่านี้จะฟังรีบ มากกว่านี้จะกลับไปเหมือนเดิม)

---

## อ่านตัวเลข วันที่ ตัวย่อ ผิด

ดู [THAI-TTS § การออกเสียง](THAI-TTS.md#การออกเสียง--แก้ที่ข้อความ-ไม่ใช่ที่โมเดล)

ตรวจว่าโมเดลได้รับข้อความอะไรจริง ๆ:

```bash
curl -s -X POST http://<node>:8769/normalize \
  -H "X-Jarvis-Token: $JARVIS_TTS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"ตอนนี้เวลา 23:45 น."}'
```

---

## เสียงออกมาเพี้ยน ฟังไม่รู้เรื่อง

เรียงตามความน่าจะเป็น:

1. **`ref_text` ไม่ตรงกับคลิป** — สาเหตุอันดับหนึ่ง ถอดคลิปด้วย STT แล้ว
   เทียบกับที่เขียนไว้
2. **คลิปอ้างอิงมีเสียงรบกวน/ก้อง** — โมเดลลอกมาหมด ลองคลิปที่สะอาดกว่า
3. **`step` ต่ำเกินไป** — ต่ำกว่า 16 มักได้เสียงสั่น ลองกลับไป 32
4. **`cfg` สูงเกินไป** — เกิน 3.0 เสียงจะแข็งและ over-articulate

```bash
# ถอดคลิปอ้างอิงเพื่อเทียบกับ ref_text
ssh rtx4000
cd ~/jarvis-gpu-node
ffmpeg -i voices/jarvis_ref.wav -f s16le -ac 1 -ar 16000 - 2>/dev/null \
  | curl -s -X POST localhost:8768/stt \
      -H "X-Jarvis-Token: $JARVIS_STT_TOKEN" --data-binary @-
```

---

## เสียงพูดเร็วหรือช้าผิดปกติ เหมือนเทปยืด

มี **สาม** ค่าที่ต้องเท่ากัน ไม่ใช่สอง — และเวลาไม่ตรงจะไม่มี error ใด ๆ
เสียงแค่เล่นผิดความเร็ว ซึ่งฟังเหมือนโมเดลแย่ ไม่เหมือน config ผิด

```bash
./scripts/healthcheck.sh 100.113.214.111   # บรรทัด "audio rate"
```

| ที่ | ค่า |
|---|---|
| GPU node | `JARVIS_TTS_PCM_RATE` ใน `gpu-node/.env` |
| host | `voice.sample_rate` ใน `server/config/server.yaml` |
| HUD | `window.TTS_RATE` ใน `server/hud/index.html` (ใส่โดย `apply_voicemode.py`) |

ปัจจุบันทั้งสามเป็น **24000** — อัตราที่ F5-TTS สร้างออกมาเอง

## คุยไปสักพักแล้ว agent ไม่ได้ยิน ต้องรีเฟรชถึงกลับมาใช้ได้

อาการคลาสสิกของ **AudioContext ที่ถูกพักไว้** ซึ่งพังสองชั้นพร้อมกัน:

```
แท็บถูกซ่อน / OS เปลี่ยนอุปกรณ์เสียง
   → AudioContext suspend
   → worklet หยุดส่งเฟรม        ← VAD ไม่ได้ยินอะไรเลย
   → currentTime หยุดเดิน
   → botSpeaking() เป็นจริงตลอดกาล   ← และ VAD ก็ยกกำแพงสูงค้างไว้ด้วย
```

`botSpeaking()` เทียบ `playhead > currentTime` · พอ context หยุด `currentTime`
แช่อยู่กับที่ ส่วน `playhead` ยังอยู่ข้างหน้า → **"บอทกำลังพูดอยู่" ตลอดไป** →
`isAgentBusy()` เป็นจริง → VAD ใช้เกณฑ์เข้มตลอด · ต่อให้เฟรมกลับมาก็ยังไม่เปิดเทิร์น

รีเฟรชแล้วหายเพราะมันสร้าง context ใหม่ทั้งหมด — ไม่ใช่เพราะเซิร์ฟเวอร์มีปัญหา

**สิ่งที่ทำไว้แล้ว** (v2):

| กลไก | จับอะไร |
|---|---|
| `micWatchdog` ทุก 2 วินาที | context ถูกพัก · track จบ (`readyState === "ended"`) · ไม่มีเฟรมเข้ามาเกิน 4 วินาที → สร้างสายสัญญาณใหม่ |
| `visibilitychange` | กลับมาที่แท็บแล้ว resume ทันที ไม่ต้องรอ watchdog |
| `botSpeaking()` | คืน false ทันทีถ้า context ไม่ได้อยู่ในสถานะ `running` |
| `socketWatchdog` ทุก 5 วินาที | websocket ที่ตายโดยไม่ยิง `onclose` — `wsReady` ค้างเป็น true แล้วทุกอย่างที่ส่งหายไปเงียบ ๆ |

**ดูสถานะได้ที่แถบบน** — จุดข้าง `MIC` · ถ้าแดงแปลว่าไม่ได้ยินเสียงเข้า และจะมี
ข้อความบอกในบทสนทนาว่ากำลังต่อใหม่ · หน้า **ระบบ** บอกละเอียดกว่า:
สถานะ context และเวลาที่เฟรมล่าสุดเข้ามากี่มิลลิวินาทีที่แล้ว

> ถ้ายังเจออยู่ ให้ดูที่หน้า "ระบบ" ว่า **เสียง** เป็น `running` ไหม และ
> **เฟรมล่าสุด** เกิน 4000 ms หรือเปล่า — สองค่านี้บอกได้ว่าเป็นชั้นไหน

## สั่งงานหนักแล้วเงียบสนิท ไม่มีอะไรกลับมาเลย

จาก turn log จริง:

```
tot=169.14s  ttfa=None  reply=0  tools=execute_code,execute_code
errors: ['turn cancelled (barge-in or stop)']
said:  "ขอรายละเอียดทั้งสิบสองรายการหน่อย"
```

เทิร์นนั้น**ทำงานจริงอยู่เกือบสามนาที** (ยิง `execute_code` เข้า ERP หลายรอบ) ·
เสียงรับทราบดังตอน 1.8 วินาที แล้ว**เงียบสนิทหลังจากนั้น** · สุดท้ายมีคนพูดแทรก
เพราะคิดว่าระบบตายไปแล้ว → **ถูกยกเลิก งานทั้งหมดหายไป ไม่มีอะไรออกมาเลย**

**ความเงียบคือรายงานที่แย่ที่สุดของงานที่กำลังทำอยู่** เพราะมันแยกไม่ออกจาก
ระบบที่ตายไปแล้ว

**สิ่งที่ทำแล้ว:**

| กลไก | เมื่อไหร่ |
|---|---|
| `/api/working` พูดว่า "กำลังทำงานอยู่ครับ" | 15 วินาทีหลังเริ่มคิด |
| พูดซ้ำ (สลับประโยค) | ทุก 30 วินาทีหลังจากนั้น |
| ตัวนับวินาทีบนจอ | เกิน 4 วินาที |
| บอกเมื่อเทิร์นถูกยกเลิก | ทันที — ไม่ปล่อยให้เงียบ |

คลิปจะดังเฉพาะตอนที่**ไม่มีเสียงอื่นออกอยู่** ทับคำตอบจริงไม่ได้ · แก้ประโยคได้ที่
`WORKING_TEXTS` ใน `host/patches/apply_handsfree.py`

> ถ้างานยาวกว่า `hermes.timeout` (ค่าเริ่มต้น 240 วินาที) เทิร์นจะจบด้วย timeout
> ไม่ใช่เงียบ · เทิร์นนี้ไม่ได้ timeout แต่ถูกพูดแทรกที่ 169 วินาที

## agent พูด "ครับ" / "อืม" / "รับทราบ" เองทั้งที่ยังไม่มีใครพูด

สองสาเหตุ คนละชั้นกัน:

**1. คลิปรับทราบเล่นเร็วเกินไป** — เคยเล่นทันทีที่ส่งเทิร์น ก่อนรู้ว่ามีเทิร์นจริง
ไหม · ดู [PERFORMANCE.md](PERFORMANCE.md#คำรับทราบ--ตอนนี้รอก่อน) · ปรับได้ที่
`VAD.ackDelayMs` (localStorage `jarvisVad`)

**2. Whisper แต่งข้อความจากความเงียบ** — โมเดลไม่คืน "ไม่มีอะไร" ให้คลิปที่ไม่มี
อะไร มันเขียนสิ่งที่คนน่าจะพูดที่สุด ซึ่งในภาษาไทยคือคำลงท้าย · ครั้งหนึ่งคลิป
เสียงรบกวนกลับมาเป็น `"เติมมาเตือน"` ซึ่งไม่ใช่ประโยคในภาษาใดเลย

STT sidecar จึงปฏิเสธ transcript ที่ตัวเองไม่เชื่อ จากสัญญาณอิสระสามอย่าง:

| ตัวแปร | ค่าเริ่มต้น | ตัดเมื่อ |
|---|---|---|
| `JARVIS_STT_NO_SPEECH_MAX` | 0.6 | Whisper บอกเองว่านี่ไม่ใช่เสียงพูด |
| `JARVIS_STT_MIN_LOGPROB` | -1.0 | decoder ไม่มั่นใจในสิ่งที่ตัวเองเขียน |
| `JARVIS_STT_DROP_FILLER` | 1 | ทั้งประโยคเป็นคำลงท้ายเปล่า ๆ |

อันที่สามคือ *backchannel* — ผู้ฟังพยักหน้ารับ · ตอบมันกลับไปคือการขัดจังหวะ
เทิร์นที่มันกำลังรับอยู่ · คลิปที่ถูกตัดคืน `text: ""` (voice server ทิ้ง
transcript ว่างอยู่แล้ว) พร้อมฟิลด์ `dropped` บอกเหตุผล และนับใน `/health`

ดูว่าตัดอะไรไปบ้าง (ข้อความที่ถูกตัดจะอยู่ใน log เท่านั้น ไม่ส่งกลับ):

```bash
ssh rtx4000 'journalctl --user -u jarvis-stt -n 50 | grep dropped'
```

ถ้าตัดของจริงทิ้งบ่อย ให้ลด `JARVIS_STT_NO_SPEECH_MAX` ลง หรือปิด
`JARVIS_STT_DROP_FILLER=0`

---

## ถอดเสียงได้ แต่ความแม่นตกลงกะทันหัน

น่าจะตกไปใช้ CPU fallback อยู่โดยไม่รู้ตัว — เกิดได้จาก token ไม่ตรง หรือ
node ไม่ตอบ

```bash
# ยิงตรงไปที่ node เพื่อดูว่า auth ผ่านไหม
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://100.113.214.111:8768/stt \
  -H "X-Jarvis-Token: $JARVIS_STT_TOKEN" --data-binary @/dev/null
```

| ผลลัพธ์ | ความหมาย |
|---|---|
| `400` | auth ผ่าน (body ว่างเลยฟ้อง) — ปกติ |
| `401` | token ไม่ตรง |
| `000` | ต่อไม่ติด — ดู firewall / Tailscale |

---

## ไม่มีเสียงออกเลย แต่ระบบไม่ error

`F5TTSProvider.stream()` ข้ามประโยคที่ล้มเหลวโดยตั้งใจ ดังนั้นความล้มเหลว
ของ TTS จะโผล่ใน log ของ host ไม่ใช่ที่ HUD

```bash
# ฝั่ง host
grep -i "jarvis.voice.f5" <log ของ voice server>
```

ทดสอบ provider แยกโดยตรง:

```bash
python host/adapters/f5_tts_provider.py \
  --url http://100.113.214.111:8769 \
  --text "ทดสอบเสียงภาษาไทย" --out /tmp/t.wav
```

---

## เชื่อมต่อ node ไม่ได้

```bash
tailscale status | grep dgx-msi-04       # node ออนไลน์ไหม
ping -c3 100.113.214.111                  # ถึงไหม
nc -vz 100.113.214.111 8768               # พอร์ตเปิดไหม
ssh rtx4000 'systemctl --user is-active jarvis-stt jarvis-tts'
```

ถ้า ping ได้แต่พอร์ตไม่เปิด มักเป็นเพราะ:

- บริการผูกกับ `127.0.0.1` แทน `0.0.0.0` หรือ tailnet IP
- firewall บน node บล็อกอยู่
- บริการยังโหลดโมเดลไม่เสร็จ (ดู `journalctl`)

---

## บริการดับหลัง logout

```bash
ssh rtx4000 'loginctl enable-linger $USER'
```

systemd user service จะถูกฆ่าเมื่อ session สุดท้ายจบ เว้นแต่เปิด linger

---

## โมเดลดาวน์โหลดไม่สำเร็จ

```bash
ssh rtx4000
cd ~/jarvis-gpu-node && source .venv/bin/activate
python -c "from f5_tts_th.tts import TTS; TTS(model='v2')"
```

ถ้าติด rate limit หรือต้องใช้ token ของ Hugging Face:

```bash
export HF_TOKEN=hf_xxx
# หรือใช้ mirror
export HF_ENDPOINT=https://hf-mirror.com
```

---

## รวบรวมข้อมูลก่อนเปิด issue

```bash
{
  echo "=== healthcheck ==="; ./scripts/healthcheck.sh
  echo "=== stt health ==="; curl -s http://100.113.214.111:8768/health
  echo "=== tts health ==="; curl -s http://100.113.214.111:8769/health
  echo "=== node ==="; ssh rtx4000 'nvidia-smi; systemctl --user status jarvis-stt jarvis-tts --no-pager'
  echo "=== recent errors ==="; ssh rtx4000 'journalctl --user -u jarvis-tts -p err -n 50 --no-pager'
} > jarvis-debug.txt 2>&1
```

> ตรวจไฟล์ก่อนแนบ — อาจมี token ปนอยู่
