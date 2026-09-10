# คู่มือปฏิบัติการ

## บริการทั้งหมดในระบบ

| เครื่อง | service | พอร์ต |
|---|---|---|
| `rtx4000` | `jarvis-stt` | 8768 |
| `rtx4000` | `jarvis-tts` | 8769 |
| `rtx4000` | `jarvis-stats` | 8767 |
| `HermesJarvis` | `jarvis-voice` | 8765 (ws) · 8766 (wss+HUD) · 9443 (dashboard proxy) |
| `HermesJarvis` | `hermes-gateway` | 8642 (Hermes API) |

ทุกตัวเป็น systemd **user** service และเปิด linger ไว้แล้ว จึงรอด reboot และ logout

## คำสั่งประจำวัน

```bash
# สถานะ GPU node
./scripts/healthcheck.sh 100.113.214.111

# ทดสอบว่าพูดและฟังได้จริง (STT + TTS)
./scripts/smoke-test.sh 100.113.214.111
```

### ทดสอบเต็มวง (บน voice host)

```bash
ssh <voice-host>
cd ~/jarvis_ai && set -a && . ~/.hermes/.env && set +a
.venv/bin/python server/scripts/ws_e2e_test.py /path/to/16k-mono.wav
```

พิมพ์ทุก event ตั้งแต่ transcript, tool ที่ agent เรียก, คำตอบ จนถึงจำนวน byte
ของเสียงที่ส่งกลับ — เป็นวิธีที่เร็วที่สุดในการดูว่าพังตรงไหน

ไม่มีไฟล์เสียงทดสอบ? สร้างจาก TTS ของเราเองได้:

```bash
curl -s http://100.113.214.111:8769/tts \
  -H "X-Jarvis-Token: $JARVIS_TTS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"ตอนนี้เวลาเท่าไหร่ครับ","format":"pcm16"}' --output cmd.pcm
python3 -c "
import wave; p=open('cmd.pcm','rb').read()
w=wave.open('cmd.wav','wb'); w.setnchannels(1); w.setsampwidth(2)
w.setframerate(16000); w.writeframes(p); w.close()"
```

## จัดการ voice server (host)

```bash
systemctl --user status  jarvis-voice
systemctl --user restart jarvis-voice
journalctl --user -u jarvis-voice -f
```

การ start ใช้เวลา ~30 วินาที (โหลดโมเดล STT สำรองบน CPU) · รอข้อความ
`STT pipeline warmed.` ก่อนถือว่าพร้อม

### หมุน HUD token

```bash
./scripts/new-hud-token.sh     # สร้างใหม่ + เขียน .env + restart ให้เอง
```

ทุกเครื่องที่เคยเข้าจะต้องใส่ token ใหม่อีกครั้ง

## จัดการบริการบน GPU node

```bash
ssh rtx4000

systemctl --user status  jarvis-stt jarvis-tts jarvis-stats
systemctl --user restart jarvis-tts
systemctl --user stop    jarvis-stt

# log สด
journalctl --user -u jarvis-tts -f

# log ย้อนหลังพร้อมเวลา
journalctl --user -u jarvis-stt --since "1 hour ago"

# เฉพาะ error
journalctl --user -u jarvis-tts -p err --since today
```

> ถ้าบริการดับทุกครั้งที่ logout แปลว่ายังไม่ได้เปิด linger:
> `loginctl enable-linger $USER`

## ดูภาระของเครื่อง

```bash
curl -s http://100.113.214.111:8767/stats | python3 -m json.tool
watch -n2 nvidia-smi
```

`/stats` รวมสถานะของ sidecar ทั้งสองไว้ด้วย — ถ้า `sidecars.tts.up` เป็น
`false` แต่ systemd บอกว่า active แปลว่ากระบวนการยังโหลดโมเดลอยู่ หรือ
โหลดล้มเหลวหลัง start สำเร็จ ให้ดู journal ต่อ

## ดูสถิติการใช้งาน

```bash
curl -s http://100.113.214.111:8769/health | python3 -m json.tool
```

```json
{
  "stats": {"requests": 412, "chars": 18730, "errors": 2, "total_seconds": 631.2}
}
```

`total_seconds ÷ requests` = เวลาสังเคราะห์เฉลี่ยต่อประโยค ถ้าค่านี้ค่อย ๆ
สูงขึ้นเรื่อย ๆ มักแปลว่ามีอย่างอื่นมาแย่ง GPU

## อัปเดต

```bash
# ฝั่ง dev
git pull
./scripts/deploy-gpu-node.sh rtx4000     # rsync + install.sh ใหม่

# บน node
ssh rtx4000
systemctl --user restart jarvis-stt jarvis-tts jarvis-stats
```

`install.sh` เป็น idempotent — รันซ้ำได้ ข้ามขั้นที่ทำไปแล้ว และจะไม่ทับ
`.env` หรือ `voices.yaml` ที่แก้ไว้

### เปลี่ยนโมเดล

```bash
ssh rtx4000 'cd ~/jarvis-gpu-node && sed -i "s/^JARVIS_STT_MODEL=.*/JARVIS_STT_MODEL=large-v3-turbo/" .env'
ssh rtx4000 'systemctl --user restart jarvis-stt'
```

น้ำหนักโมเดลใหม่จะถูกดาวน์โหลดตอน start ครั้งแรก ซึ่งอาจใช้เวลาหลายนาที —
`/health` จะตอบ `loading` ระหว่างนั้น

## สำรองข้อมูล

สิ่งที่มีค่าจริง ๆ บน node มีน้อยมาก:

| ต้องสำรอง | ไม่ต้อง |
|---|---|
| `voices/*.wav` — คลิปอ้างอิง | `.venv/` — สร้างใหม่จาก `install.sh` |
| `voices.yaml` | น้ำหนักโมเดล — ดาวน์โหลดใหม่ได้ |
| `.env` — token | |

```bash
ssh rtx4000 'tar czf - -C ~/jarvis-gpu-node voices voices.yaml .env' \
  > jarvis-node-backup-$(date +%Y%m%d).tar.gz
```

> ไฟล์นี้มี token อยู่ข้างใน — เก็บให้เหมือนเก็บรหัสผ่าน

## เมื่อ GPU node ดับ

ระบบจะเสื่อมสภาพแบบไม่เท่ากันสองฝั่ง — ตั้งใจให้เป็นแบบนี้:

| ส่วน | เมื่อ node ดับ |
|---|---|
| **หู (STT)** | ตกไปใช้โมเดล CPU บน host อัตโนมัติ ยังใช้งานได้ ความแม่นตก |
| **ปาก (TTS)** | ไม่มีเสียงออก — ไม่มี fallback |

ไม่มี fallback ให้ TTS เพราะไม่มี TTS ภาษาไทยที่รันบน CPU แล้วยังฟังได้จริง
การใส่ fallback ที่เสียงแย่มากจะทำให้ผู้ใช้สับสนว่าระบบพังหรือไม่ ซึ่งแย่กว่า
การเงียบแล้วเห็น error ชัด ๆ

ถ้าต้องการให้ระบบยังตอบได้ตอน node ดับ ทางที่ควรทำคือให้ HUD แสดงคำตอบเป็น
ข้อความแทนเสียง

## เช็กลิสต์ประจำสัปดาห์

```bash
./scripts/healthcheck.sh                                    # ทุกอย่างขึ้น
curl -s http://100.113.214.111:8769/health | grep errors     # error สะสมเพิ่มไหม
ssh rtx4000 'df -h ~ | tail -1'                               # ดิสก์เหลือพอไหม
ssh rtx4000 'journalctl --user -u jarvis-tts -p err --since "7 days ago" | tail'
```
