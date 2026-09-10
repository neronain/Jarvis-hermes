# แก้ปัญหา

เรียงจากอาการที่พบบ่อยที่สุด แต่ละหัวข้อไล่จาก **อาการ → สาเหตุที่เป็นไปได้ →
วิธียืนยัน → วิธีแก้**

---

## ทุกอย่างช้ามาก — ทั้งที่มี GPU

**สาเหตุที่พบบ่อยที่สุดคือ torch ที่ติดตั้งเป็น CPU build** ซึ่งจะไม่ error
เลย แค่ช้าลง 20–50 เท่า

```bash
ssh msi-4
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
ssh msi-4 'ls -la ~/jarvis-gpu-node/voices/'
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

## TTS ตอบ 500 — `TorchCodec is required for load_with_torchcodec`

torchaudio ตั้งแต่ 2.9 เลิกมี decoder ของตัวเองและไปเรียก `torchcodec` ซึ่ง
ต้องมี FFmpeg ระดับระบบ · `tts_server.py` ตรวจเรื่องนี้ตอนบูตและสลับไปใช้
`soundfile` แทนอัตโนมัติ ถ้ายังเจอ error นี้แปลว่ารันโค้ดเวอร์ชันเก่าอยู่:

```bash
ssh msi-4 'cd ~/Jarvis-hermes && git pull && systemctl --user restart jarvis-tts'
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

## เสียงออกมาเพี้ยน ฟังไม่รู้เรื่อง

เรียงตามความน่าจะเป็น:

1. **`ref_text` ไม่ตรงกับคลิป** — สาเหตุอันดับหนึ่ง ถอดคลิปด้วย STT แล้ว
   เทียบกับที่เขียนไว้
2. **คลิปอ้างอิงมีเสียงรบกวน/ก้อง** — โมเดลลอกมาหมด ลองคลิปที่สะอาดกว่า
3. **`step` ต่ำเกินไป** — ต่ำกว่า 16 มักได้เสียงสั่น ลองกลับไป 32
4. **`cfg` สูงเกินไป** — เกิน 3.0 เสียงจะแข็งและ over-articulate

```bash
# ถอดคลิปอ้างอิงเพื่อเทียบกับ ref_text
ssh msi-4
cd ~/jarvis-gpu-node
ffmpeg -i voices/jarvis_ref.wav -f s16le -ac 1 -ar 16000 - 2>/dev/null \
  | curl -s -X POST localhost:8768/stt \
      -H "X-Jarvis-Token: $JARVIS_STT_TOKEN" --data-binary @-
```

---

## เสียงพูดเร็วหรือช้าผิดปกติ เหมือนเทปยืด

`JARVIS_TTS_PCM_RATE` บน node ไม่ตรงกับ `voice.sample_rate` ฝั่ง host

```bash
curl -s http://100.84.136.110:8769/health | grep pcm_rate
grep sample_rate <jarvis_ai>/server/config/server.yaml
```

สองค่านี้ต้องเท่ากัน

---

## ถอดเสียงได้ แต่ความแม่นตกลงกะทันหัน

น่าจะตกไปใช้ CPU fallback อยู่โดยไม่รู้ตัว — เกิดได้จาก token ไม่ตรง หรือ
node ไม่ตอบ

```bash
# ยิงตรงไปที่ node เพื่อดูว่า auth ผ่านไหม
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://100.84.136.110:8768/stt \
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
  --url http://100.84.136.110:8769 \
  --text "ทดสอบเสียงภาษาไทย" --out /tmp/t.wav
```

---

## เชื่อมต่อ node ไม่ได้

```bash
tailscale status | grep dgx-msi-04       # node ออนไลน์ไหม
ping -c3 100.84.136.110                  # ถึงไหม
nc -vz 100.84.136.110 8768               # พอร์ตเปิดไหม
ssh msi-4 'systemctl --user is-active jarvis-stt jarvis-tts'
```

ถ้า ping ได้แต่พอร์ตไม่เปิด มักเป็นเพราะ:

- บริการผูกกับ `127.0.0.1` แทน `0.0.0.0` หรือ tailnet IP
- firewall บน node บล็อกอยู่
- บริการยังโหลดโมเดลไม่เสร็จ (ดู `journalctl`)

---

## บริการดับหลัง logout

```bash
ssh msi-4 'loginctl enable-linger $USER'
```

systemd user service จะถูกฆ่าเมื่อ session สุดท้ายจบ เว้นแต่เปิด linger

---

## โมเดลดาวน์โหลดไม่สำเร็จ

```bash
ssh msi-4
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
  echo "=== stt health ==="; curl -s http://100.84.136.110:8768/health
  echo "=== tts health ==="; curl -s http://100.84.136.110:8769/health
  echo "=== node ==="; ssh msi-4 'nvidia-smi; systemctl --user status jarvis-stt jarvis-tts --no-pager'
  echo "=== recent errors ==="; ssh msi-4 'journalctl --user -u jarvis-tts -p err -n 50 --no-pager'
} > jarvis-debug.txt 2>&1
```

> ตรวจไฟล์ก่อนแนบ — อาจมี token ปนอยู่
