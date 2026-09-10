# คู่มือปฏิบัติการ

## คำสั่งประจำวัน

```bash
# สถานะทั้งระบบ
./scripts/healthcheck.sh

# ทดสอบว่าพูดและฟังได้จริง
./scripts/smoke-test.sh
```

## จัดการบริการบน GPU node

```bash
ssh msi-4

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
curl -s http://100.84.136.110:8767/stats | python3 -m json.tool
watch -n2 nvidia-smi
```

`/stats` รวมสถานะของ sidecar ทั้งสองไว้ด้วย — ถ้า `sidecars.tts.up` เป็น
`false` แต่ systemd บอกว่า active แปลว่ากระบวนการยังโหลดโมเดลอยู่ หรือ
โหลดล้มเหลวหลัง start สำเร็จ ให้ดู journal ต่อ

## ดูสถิติการใช้งาน

```bash
curl -s http://100.84.136.110:8769/health | python3 -m json.tool
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
./scripts/deploy-gpu-node.sh msi-4     # rsync + install.sh ใหม่

# บน node
ssh msi-4
systemctl --user restart jarvis-stt jarvis-tts jarvis-stats
```

`install.sh` เป็น idempotent — รันซ้ำได้ ข้ามขั้นที่ทำไปแล้ว และจะไม่ทับ
`.env` หรือ `voices.yaml` ที่แก้ไว้

### เปลี่ยนโมเดล

```bash
ssh msi-4 'cd ~/jarvis-gpu-node && sed -i "s/^JARVIS_STT_MODEL=.*/JARVIS_STT_MODEL=large-v3-turbo/" .env'
ssh msi-4 'systemctl --user restart jarvis-stt'
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
ssh msi-4 'tar czf - -C ~/jarvis-gpu-node voices voices.yaml .env' \
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
curl -s http://100.84.136.110:8769/health | grep errors     # error สะสมเพิ่มไหม
ssh msi-4 'df -h ~ | tail -1'                               # ดิสก์เหลือพอไหม
ssh msi-4 'journalctl --user -u jarvis-tts -p err --since "7 days ago" | tail'
```
