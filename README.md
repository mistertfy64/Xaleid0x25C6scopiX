# Vital Sign Monitor Project

## สิ่งที่พัฒนาแล้ว: Sensor Node, Gateway และ Cloud

### Sensor Node (ESP32)

- อ่านชีพจรและ SpO₂ จาก MAX30102 และอุณหภูมิผิวหนังจาก GY-906 MLX90614 ผ่าน I2C bus ความเร็ว 100 kHz (ถ้าไม่มี MLX90614 จะส่ง `body_temp: null` และลองเชื่อมต่อใหม่ทุก 10 วินาที)
- ส่งข้อมูล JSON ทุก 2 วินาทีผ่าน Wi-Fi ไปยัง MQTT broker ของ Gateway บน topic `vsm/sensor/<node_id>` พร้อม `node_id`, `seq`, `finger` และ `uptime_ms`
- แยกค่า Wi-Fi และ MQTT credentials ไว้ใน `sensor/node/secrets.h` (git-ignored; ดูตัวอย่างใน `sensor/node/secrets.example.h`)

### Gateway

- รับข้อมูลจาก ESP32 ผ่าน MQTT (ค่าเริ่มต้น subscribe ที่ `vsm/sensor/+`) หรือเลือกใช้ข้อมูลจาก serial (`--port`) หรือข้อมูลจำลอง (`--fake`) และเติมค่าอุณหภูมิเครื่องจาก LM35 ผ่าน ADS1115 โดยเฉลี่ย 5 ตัวอย่าง; หากไม่มีอุปกรณ์หรือไลบรารี ค่าจะเป็น `None`
- รวม reading เป็นชุดละ `--n` ค่า (ค่าเริ่มต้น 10 ค่า) แล้วบันทึกลงคิว SQLite พร้อม `session_id` (idempotency key) ก่อนอัปโหลด เพื่อไม่ให้ข้อมูลหายหรือซ้ำเมื่อเครือข่ายขัดข้องหรือ gateway รีสตาร์ต ระบบจะลองส่งซ้ำด้วย exponential backoff สำหรับ network error, timeout, HTTP 5xx และ HTTP 429 โดยเว้นช่วงสูงสุด 5 นาที
- เข้าสู่ระบบใหม่ทุกครั้งก่อนส่งข้อมูลในคิว เพราะ gateway token ใช้ได้ครั้งเดียว เปิดโหมดเก็บข้อมูลต่อเนื่องด้วย `--loop` ได้
- อ่าน URL, ข้อมูลบัญชี Cloud และค่า MQTT broker จาก environment หรือ `.env` และบังคับใช้ HTTPS พร้อมตรวจสอบ certificate
- มี systemd service สำหรับ Raspberry Pi OS ซึ่งเริ่มทำงานตอนบูตหลัง `mosquitto.service` และเปิด `--loop`

### Cloud backend และ Dashboard

- ใช้ FastAPI และ SQLite โดยรองรับ `session_id` แบบ optional เพื่อป้องกันการบันทึกข้อมูลซ้ำเมื่อ Gateway retry
- gateway token ใช้ครั้งเดียว ส่วน viewer token ใช้ซ้ำได้ภายในอายุ token; มีการตรวจและล้าง token ที่หมดอายุ
- จำกัดการลองเข้าสู่ระบบ 5 ครั้งต่อนาทีต่อ IP ต่อ backend process และกำหนด CORS origins จาก environment
- Dashboard ภาษาไทย (`cloud/dashboard/index.html`) แสดงค่าล่าสุด กราฟแนวโน้ม ตารางประวัติ พร้อมรองรับค่า `null` และการกรองข้อมูลตาม `node_id`
- เพิ่ม Dockerfile ที่ใช้ Python 3.11 รันด้วย non-root user และเก็บฐานข้อมูลใน `/data` พร้อมคำแนะนำการวางหลัง HTTPS reverse proxy ด้วย Caddy

### การทดสอบ

ชุดทดสอบครอบคลุมการเข้าสู่ระบบ, การใช้ gateway token ครั้งเดียว, การปฏิเสธ userID ที่ไม่ถูกต้อง, การป้องกันข้อมูลซ้ำด้วย `session_id`, การใช้ viewer token ซ้ำ และ token หมดอายุ รวมถึงการรัน gateway โหมดปกติ, `--loop`, คิว SQLite และโหมด MQTT

ติดตั้ง dependencies สำหรับทดสอบและรันจากโฟลเดอร์หลักของ repository:

```sh
python -m pip install -r cloud/requirements-dev.txt
python -m pip install -r gateway/requirements-dev.txt
python -m pytest -q cloud/tests gateway/tests
```

การทำงานกับ Raspberry Pi/ADS1115 จริง, systemd บนอุปกรณ์จริง และการ build Docker image ยังต้องทดสอบในสภาพแวดล้อมปลายทาง
