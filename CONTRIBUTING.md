# การมีส่วนร่วม

## ก่อนส่ง PR

```bash
# ตรวจ syntax ทุกไฟล์
python3 -m compileall -q gpu-node host
for f in scripts/*.sh gpu-node/*.sh; do bash -n "$f" || exit 1; done

# ถ้ามี GPU node ให้ทดสอบจริง
./scripts/healthcheck.sh
./scripts/smoke-test.sh
```

## แนวทางของโค้ดในโปรเจกต์นี้

- **รักษาสัญญากับ upstream** — `POST /stt` ต้องคงรูปแบบเดิมเสมอ การเพิ่มฟิลด์
  ใน response ทำได้ แต่ห้ามเปลี่ยนหรือลบของเดิม
- **ตรวจ config ตอนบูต ไม่ใช่ตอน request** — config ผิดควรทำให้ service ไม่ขึ้น
  พร้อมข้อความที่บอกว่าผิดตรงไหน ดีกว่าไปพังตอนผู้ใช้กำลังพูด
- **ความล้มเหลวบางส่วนไม่ควรล้มทั้งเทิร์น** — ประโยคที่สังเคราะห์ไม่ได้ให้ข้าม
  และ log ไว้
- **ความลับอยู่ใน env เท่านั้น** — ห้ามมี default ที่เป็นค่าจริงในโค้ดหรือ YAML
- **คอมเมนต์อธิบายว่าทำไม ไม่ใช่ว่าอะไร** — โค้ดบอกได้อยู่แล้วว่ามันทำอะไร

## เอกสาร

ทุกการเปลี่ยนแปลงที่กระทบผู้ใช้ต้องอัปเดตเอกสารในชุดเดียวกับ PR:

| เปลี่ยนอะไร | ต้องแก้ |
|---|---|
| เพิ่ม/แก้ตัวแปร env | `docs/CONFIGURATION.md` + `.env.example` |
| เปลี่ยน endpoint | `docs/ARCHITECTURE.md` + `README.md` |
| เพิ่มขั้นตอนติดตั้ง | `docs/DEPLOYMENT.md` |
| เจอ failure mode ใหม่ | `docs/TROUBLESHOOTING.md` |
