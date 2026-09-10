# ประสิทธิภาพ

ตัวเลขทั้งหมดวัดจริงบนระบบที่ deploy แล้ว: GPU node = RTX PRO 4000 Blackwell
(sm_120), voice host = OrbStack VM `aarch64` ไม่มี GPU, เชื่อมกันผ่าน Tailscale

## แยกตามขั้นตอน

| ขั้นตอน | เวลา | หมายเหตุ |
|---|---|---|
| เครือข่าย host → GPU node | ~20 ms | Tailscale RTT |
| **STT** (`large-v3` float16) | **0.50 s** | คลิป 3 วินาที → RTF **8.3×** |
| **LLM ดิบ** (ยิงตรงไป gateway) | **0.93 s** | `gemma4-26b`, ttfb |
| **Hermes agent เต็มเทิร์น** | **~5–7 s** | ส่วนที่เหลือคือ overhead ของ agent |
| **TTS** (F5-TTS-TH v2) | **0.59–1.02 s** | เร็วกว่า realtime 3.4–7× |
| **รวม พูด → ได้ยินเสียงตอบ** | **6–9 s** | |

## คอขวดอยู่ที่ agent ไม่ใช่ระบบเสียง

นี่คือข้อสรุปที่สำคัญที่สุดของหน้านี้ · เอา 0.50 (STT) + 0.93 (LLM) + 0.59
(TTS) = **~2 วินาที** แต่ของจริงคือ 6–9 วินาที ส่วนต่าง **~5 วินาที** เกิดขึ้น
ระหว่างที่ Hermes ประกอบเทิร์น — system prompt ที่รวม skill กว่า 80 ตัว,
การดึง memory, การประกอบ tool schema, session handling

**การจูนโมเดลเสียงจะไม่ช่วย** ลด `step` ของ TTS จาก 32 เหลือ 24 ประหยัดได้
~0.2 วินาทีจากทั้งหมด 7 วินาที · ที่คุ้มกว่าคือลดภาระของ agent:

| วิธี | ผลที่คาด | ผลข้างเคียง |
|---|---|---|
| จำกัด toolset ของ session เสียง | มาก — prompt สั้นลงชัดเจน | agent ทำอะไรได้น้อยลงตอนคุยด้วยเสียง |
| `agent.reasoning_effort: low` | ปานกลาง | คำตอบคิดน้อยลง กระทบทุกการใช้งานของ Hermes ไม่ใช่แค่เสียง |
| สั่งให้ตอบสั้นใน `hermes.instructions` | ปานกลาง | ทำอยู่แล้ว — คำตอบสั้นสังเคราะห์เร็วกว่า |
| ใช้โมเดลเล็กลงสำหรับ session เสียง | มาก | คุณภาพคำตอบลด |

> ทั้งหมดนี้เป็นการตัดสินใจเรื่องพฤติกรรมของ agent ซึ่งกระทบการใช้งานอื่น
> ด้วย จึงไม่ได้ตั้งค่าไว้ให้ล่วงหน้า

## `ack_after_seconds` ยังไม่ทำงาน

`server.example.yaml` ของ upstream มีคีย์ `ack_after_seconds` และ `ack_texts`
สำหรับพูดคำรับทราบระหว่างที่ agent กำลังคิด ซึ่งเป็นวิธีแก้ที่ตรงกับปัญหานี้
ที่สุด — **แต่ `server.py` เวอร์ชันปัจจุบันยังไม่ได้ implement** (grep หา
`ack_after` ใน `server.py` แล้วไม่เจอเลย)

ตั้งค่าไว้เท่าไหร่ก็ไม่มีผล · ถ้าอยากได้จริงต้องเขียนเพิ่มเอง: จับเวลาใน
`stream_response_audio` แล้วถ้าเกินเกณฑ์ก่อนได้ token แรก ให้ส่งประโยครับทราบ
เข้า TTS ก่อน

## วิธีวัดเอง

```bash
# แยกทีละขั้น
./scripts/smoke-test.sh <gpu-node>          # STT + TTS อย่างเดียว
curl -sD- -o /dev/null -X POST http://<node>:8769/tts \
  -H "X-Jarvis-Token: $JARVIS_TTS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"ทดสอบ"}' | grep -i x-jarvis

# เต็มวง (บน voice host)
cd ~/jarvis_ai && .venv/bin/python server/scripts/ws_e2e_test.py test.wav
```

`X-Jarvis-Audio-Seconds ÷ X-Jarvis-Latency` = real-time factor · ต่ำกว่า 1
แปลว่าสังเคราะห์ช้ากว่าเวลาพูดจริง ซึ่งจะสะสมและรู้สึกได้ทันที
