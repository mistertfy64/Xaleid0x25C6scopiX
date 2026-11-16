# Vital Sign Monitor Project

## สิ่งที่พัฒนาแล้ว: Gateway และ Cloud

### Gateway

- อ่านข้อมูล JSON จาก serial หรือใช้ข้อมูลจำลองด้วย `--fake` และเพิ่มค่าอุณหภูมิเครื่องจาก LM35 ผ่าน ADS1115 โดยเฉลี่ย 5 ตัวอย่าง; หากไม่มีอุปกรณ์หรือไลบรารี ค่าจะเป็น `None`
- บันทึกแต่ละ session ลงคิว SQLite ในเครื่องก่อนอัปโหลด เพื่อไม่ให้ข้อมูลหายเมื่อเครือข่ายขัดข้องหรือ gateway รีสตาร์ต ระบบจะลองส่งซ้ำด้วย exponential backoff สำหรับ network error, timeout, HTTP 5xx และ HTTP 429 โดยเว้นช่วงสูงสุด 5 นาที
- เข้าสู่ระบบใหม่ทุกครั้งก่อนส่งข้อมูลในคิว เพราะ gateway token ใช้ได้ครั้งเดียว เปิดโหมดเก็บข้อมูลต่อเนื่องด้วย `--loop` ได้
- อ่าน URL และข้อมูลบัญชีจาก environment หรือ `.env` และบังคับใช้ HTTPS พร้อมตรวจสอบ certificate
- มี systemd service สำหรับ Raspberry Pi OS ซึ่งเริ่มทำงานตอนบูตและเปิด `--loop`

การส่งข้อมูลเป็นแบบ at-least-once หาก Cloud บันทึกข้อมูลแล้วแต่ response สูญหาย การลองส่งซ้ำอาจสร้างข้อมูลซ้ำได้ เพราะ API ปัจจุบันยังไม่มี idempotency key

### Cloud backend

- ใช้ FastAPI และ SQLite โดยคง API fields เดิม
- gateway token ใช้ครั้งเดียว ส่วน viewer token ใช้ซ้ำได้ภายในอายุ token; มีการตรวจและล้าง token ที่หมดอายุ
- จำกัดการลองเข้าสู่ระบบ 5 ครั้งต่อนาทีต่อ IP ต่อ backend process และกำหนด CORS origins จาก environment
- เพิ่ม Dockerfile ที่ใช้ Python 3.11 รันด้วย non-root user และเก็บฐานข้อมูลใน `/data`
- เพิ่มคำแนะนำการวาง backend หลัง HTTPS reverse proxy ด้วย Caddy

### การทดสอบ

ชุดทดสอบครอบคลุมการเข้าสู่ระบบ, การใช้ gateway token ครั้งเดียว, การปฏิเสธ userID ที่ไม่ถูกต้อง, การใช้ viewer token ซ้ำ และ token หมดอายุ รวมถึงการรัน gateway แบบครั้งเดียวและแบบต่อเนื่อง

ติดตั้ง dependencies สำหรับทดสอบและรันจากโฟลเดอร์หลักของ repository:

```sh
python -m pip install -r cloud/requirements-dev.txt
python -m pip install -r gateway/requirements-dev.txt
python -m pytest -q cloud/tests gateway/tests
```

การทำงานกับ Raspberry Pi/ADS1115 จริง, systemd บนอุปกรณ์จริง และการ build Docker image ยังต้องทดสอบในสภาพแวดล้อมปลายทาง
