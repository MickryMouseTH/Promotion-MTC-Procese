# Promotion-MTC-Procese

โปรแกรมประมวลผลรายการผ่านทาง (Toll transactions) และจับคู่โปรโมชันตามทะเบียนรถ + จังหวัด
แล้วบันทึกลงฐานข้อมูลโปรโมชัน

- **Author:** Chayanon Auttaniti
- **Program version:** 1.3.0
- **Language:** Python 3
- **ไฟล์หลัก:** `Promotion-MTC-Procese.py`
- **ไลบรารีร่วม:** `LogLibrary.py` (จัดการ config + logging + เข้ารหัสความลับ)

---

## 1. ภาพรวมโครงสร้างโปรแกรม

```
Promotion-MTC-Procese.py        # โปรแกรมหลัก (วนทำงานเป็น service)
└── LogLibrary.py               # โหลด config (JSON) + ตั้งค่า Loguru + เข้ารหัสรหัสผ่าน
    ├── Load_Config(...)        # อ่าน/สร้าง <Program>_config.json และ merge กับ default
    └── Loguru_Logging(...)     # ตั้งค่า log ลงไฟล์ logs/<Program>_<Version>.log
```

ไฟล์ที่ถูกสร้างขึ้นอัตโนมัติเมื่อรันครั้งแรก:

| ไฟล์ | คำอธิบาย |
|------|----------|
| `Promotion-MTC-Procese_config.json` | ไฟล์ตั้งค่า (รหัสผ่านจะถูกเข้ารหัสเป็น `ENC:...`) |
| `Promotion-MTC-Procese.key` | กุญแจเข้ารหัส (ผูกกับเครื่อง — ห้ามย้ายไปเครื่องอื่น) |
| `logs/Promotion-MTC-Procese_1.3.0.log` | ไฟล์ log (หมุนตามขนาด + เก็บย้อนหลังตามจำนวนวัน) |

---

## 2. ลำดับการทำงาน (Flow)

โปรแกรมทำงานวนไม่รู้จบ (loop) ทุก `RETRY_INTERVAL` วินาที:

1. **`get_Toll_Transactions`** — ดึงรายการผ่านทางจาก Toll DB
   (เฉพาะที่ยังไม่เคยทำโปรโมชัน `DMTPX_PROMOTION_DATE IS NULL`)
2. **`get_Promotion_Register`** — ดึงทะเบียนที่ลงทะเบียนโปรโมชัน (สถานะ `A`)
3. **จับคู่** — เทียบ `(ทะเบียน, จังหวัด)` แบบ normalize (strip + lower) ผ่าน lookup set
4. **`insert_Toll_Transactions`** — เพิ่มรายการที่จับคู่ได้ลง `PROMOTION_TOLL`
   (ข้ามรายการที่มี `TRANS_NO` อยู่แล้ว)
5. **`update_Toll_Transactions`** — ปั๊ม `DMTPX_PROMOTION_DATE = getdate()`
   ในรายการที่ดึงมาทั้งหมด เพื่อไม่ให้ถูกดึงซ้ำในรอบถัดไป

---

## 3. การตั้งค่า (`default_config`)

```jsonc
{
  "Toll_DB": {
    "DB_SERVER": "", "DB_DATABASE": "", "DB_USERNAME": "", "DB_PASSWORD": "",
    "ODBC_Driver": "ODBC Driver 17 for SQL Server",
    "QUERY_LIMIT": 100,        // จำนวนรายการสูงสุดต่อรอบ (TOP N)
    "Back_date": 7,            // ย้อนหลังกี่วัน
    "Back_Time": 10,           // เว้นรายการล่าสุดกี่นาที
    "Start_Date": "2025-08-07 00:00:00.000"
  },
  "Promotion_DB": {
    "DB_SERVER": "", "DB_DATABASE": "", "DB_USERNAME": "", "DB_PASSWORD": "",
    "ODBC_Driver": "ODBC Driver 17 for SQL Server"
  },
  "RETRY_INTERVAL": 10,        // วินาทีระหว่างรอบ
  "log_Level": "DEBUG",
  "Log_Console": 1,            // 1 = แสดง log บนหน้าจอด้วย
  "log_Backup": 90,            // เก็บ log ย้อนหลัง (วัน)
  "Log_Size": "10 MB"          // ขนาดไฟล์ก่อนหมุน
}
```

> **หมายเหตุความลับ:** คีย์ใดก็ตามที่มีคำว่า `pass` (เช่น `DB_PASSWORD`) จะถูก
> เข้ารหัสลงไฟล์อัตโนมัติหลังรันครั้งแรก ใส่ค่า plaintext แค่ครั้งแรกครั้งเดียว
> ขณะรันโปรแกรมจะได้ค่าที่ถอดรหัสแล้วเสมอ

---

## 4. ความเข้ากันได้กับ `LogLibrary.py` ใหม่ ✅

ตรวจสอบแล้ว — **เข้ากันได้สมบูรณ์** ไม่ต้องแก้โค้ดฝั่ง main เพื่อให้ใช้กับไลบรารีใหม่ได้:

| รายการ | สถานะ |
|--------|-------|
| `from LogLibrary import Load_Config, Loguru_Logging` | ✅ ฟังก์ชันทั้งสองยังมีอยู่ |
| `Load_Config(default_config, Program_Name)` | ✅ ลายเซ็นตรงกัน |
| `Loguru_Logging(config, Program_Name, Program_Version)` | ✅ ลายเซ็นตรงกัน |
| config แบบซ้อน (`Toll_DB`/`Promotion_DB`) | ✅ `_deep_merge` รองรับ nested dict |
| เข้ารหัส `DB_PASSWORD` ใน dict ซ้อน | ✅ `_process_secrets` วน recursive ครอบคลุม |

**สิ่งใหม่ที่ได้เพิ่มจากไลบรารีเวอร์ชันนี้:**
- เข้ารหัสรหัสผ่านในไฟล์ config ด้วย Fernet + กุญแจถูกห่อด้วย ChaCha20-Poly1305 ผูกกับเครื่อง
- เขียน config แบบ atomic (กันไฟล์เสียถ้าโปรแกรมถูกตัดกลางคัน)
- ทนต่อไฟล์ config เสีย/หาย (สำรองไฟล์เดิม `.bak` แล้วใช้ค่า default แทน)

**สิ่งที่ต้องเตรียม:**
- ต้องติดตั้งแพ็กเกจ `cryptography` เพื่อให้การเข้ารหัสทำงาน
  (`pip install cryptography`) — ถ้าไม่มี โปรแกรมยังทำงานได้แต่จะเก็บรหัสผ่านเป็น plaintext และเตือนทาง stderr
- ไฟล์ `*.key` ผูกกับเครื่อง ห้ามคัดลอกข้ามเครื่อง หากต้องย้ายให้ตั้ง env var `LOGLIB_KEY` แทน

---

## 5. รายการบั๊กที่แก้ไข (เวอร์ชัน 1.3.0)

| # | ตำแหน่ง | ปัญหาเดิม | การแก้ไข |
|---|---------|-----------|----------|
| 1 | `get_Toll_Transactions` (SQL `WHERE`) | `(X IS NOT NULL **or** X <> '')` — เงื่อนไขนี้ปล่อยให้ค่าสตริงว่าง (`''`) ผ่านได้ ทำให้ดึงรายการที่ทะเบียน/จังหวัดว่างเปล่ามาด้วย | เปลี่ยนเป็น `(X IS NOT NULL **AND** X <> '')` เพื่อกรองทั้ง NULL และค่าว่างจริง |
| 2 | main loop | log บอกว่ารอ `RETRY_INTERVAL` (default `10`) แต่ `time.sleep` ใช้ default `3600` — ค่าไม่ตรงกัน ทำให้ log เข้าใจผิดได้ | คำนวณ `retry_interval` ครั้งเดียวแล้วใช้ทั้ง log และ sleep |
| 3 | `get_Tolldb_connection` | ค่า default ของ `ODBC_Driver` เป็น `18` ขณะที่ config และ Promotion ใช้ `17` — ไม่สอดคล้องกัน | ปรับ default เป็น `ODBC Driver 17 for SQL Server` ให้ตรงกัน |
| 4 | จับคู่โปรโมชัน (`main`) | วนลูปซ้อน O(transactions × promotions) และ normalize ค่าฝั่งโปรโมชันใหม่ทุกแถว — ช้าเมื่อข้อมูลเยอะ | สร้าง lookup `set` ของ `(ทะเบียน, จังหวัด)` ครั้งเดียว แล้วเช็คแบบ O(1) |
| 5 | `insert_/update_Toll_Transactions` | ถ้าเกิด exception กลางคัน connection ไม่ถูกปิด (resource leak) และไม่มี rollback | ใช้ `try/except/finally` ปิด connection เสมอ + `rollback()` เมื่อ error |
| 6 | ทั้งสองฟังก์ชัน connection | log `Connection string` แบบเต็ม ทำให้ **รหัสผ่านโผล่ในไฟล์ log** | เปลี่ยนเป็น log แค่ server/database ไม่รวมรหัสผ่าน |

> เวอร์ชันก่อนหน้า (1.2) ได้แก้บั๊ก `'NoneType' object has no attribute 'strip'`
> ด้วยฟังก์ชัน `normalize_text()` ที่จัดการค่า `None` ไปแล้ว

---

## 6. การติดตั้งและรัน

```bash
# 1) ติดตั้ง dependency
pip install -r requirements.txt

# 2) รันครั้งแรกเพื่อสร้างไฟล์ config (จะได้ค่าว่าง)
python Promotion-MTC-Procese.py

# 3) แก้ Promotion-MTC-Procese_config.json ใส่ค่าเชื่อมต่อ DB + รหัสผ่าน (plaintext)
#    แล้วรันอีกครั้ง — รหัสผ่านจะถูกเข้ารหัสให้อัตโนมัติ
python Promotion-MTC-Procese.py
```

หยุดโปรแกรมด้วย `Ctrl+C` (จับ `KeyboardInterrupt` และปิดตัวอย่างเรียบร้อย)

---

## 7. Build เป็น executable ไฟล์เดียว (PyInstaller `--onefile`)

| ไฟล์ | ใช้บน |
|------|-------|
| `requirements.txt` | รายการ dependency สำหรับ runtime |
| `build.sh` | สร้าง executable บน **macOS / Linux** |
| `build.bat` | สร้าง executable บน **Windows** |

```bash
# macOS / Linux
./build.sh

# Windows
build.bat
```

ผลลัพธ์จะอยู่ที่ `dist/Promotion-MTC-Procese` (หรือ `.exe` บน Windows)

> สคริปต์จะติดตั้ง dependency + PyInstaller ให้อัตโนมัติ แล้วสั่ง build ด้วย
> `--onefile` พร้อม `--collect-submodules cryptography` และ `--hidden-import pyodbc`
> เพื่อให้แพ็กเกจเข้ารหัสและ ODBC ถูกรวมเข้าไปครบ
>
> **สำคัญ:** เมื่อรัน executable ไฟล์ `config.json`, `*.key` และโฟลเดอร์ `logs/`
> จะถูกสร้าง/อ่านจากตำแหน่งเดียวกับตัว executable (รองรับ `sys.frozen` ใน `LogLibrary.py`)
> ดังนั้นควรวาง executable ไว้ในโฟลเดอร์ที่เขียนไฟล์ได้

---

## 8. ตารางฐานข้อมูลที่เกี่ยวข้อง

| ฐานข้อมูล | ตาราง | บทบาท |
|-----------|-------|-------|
| Toll_DB | `DMT_PASSING_TRANSACTION` | รายการผ่านทาง (อ่าน + ปั๊ม `DMTPX_PROMOTION_DATE`) |
| Toll_DB | `DMT_PROVINCE` | แม็พรหัสจังหวัด → ชื่อจังหวัด |
| Promotion_DB | `PROMOTION_MTC_REGISTER` | ทะเบียนที่ลงทะเบียนโปรโมชัน (สถานะ `A`) |
| Promotion_DB | `PROMOTION_TOLL` | ปลายทางบันทึกรายการที่ได้โปรโมชัน (สถานะเริ่มต้น `W`) |
