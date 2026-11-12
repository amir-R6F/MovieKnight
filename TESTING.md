# راهنمای تست‌های LANWatch

این پروژه علاوه بر برنامه‌ی اصلی، یک مجموعه تست کامل دارد که **بدون mpv** و بدون
فایل ویدیو اجرا می‌شود. تست‌ها روی لینوکس (CI) و ویندوز یکسان کار می‌کنند، چون
به‌جای mpv از یک پخش‌کننده‌ی ساختگی در حافظه (`FakePlayer`) و به‌جای پورت ثابت از
پورت تصادفی سیستم (`port=0`) استفاده می‌کنند.

## ۱) نصب پیش‌نیازهای تست

اگر قبلاً محیط مجازی را ساخته‌اید (همان `.venv` که `run.bat` می‌سازد):

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

این فایل خودش `lanwatch\requirements.txt` را هم نصب می‌کند (`-r`)، پس یک دستور کافی است.
روی لینوکس به‌جای `.venv\Scripts\python.exe` از `.venv/bin/python` استفاده کنید.

پیش‌نیازهای تست:

| بسته | کاربرد |
|---|---|
| `pytest` | اجرای تست‌ها |
| `pytest-asyncio` | تست‌های `async` بدون `asyncio.run` (حالت `auto`) |
| `pytest-timeout` | هر تست حداکثر ۳۰ ثانیه؛ تستِ هنگ‌کرده شکست می‌خورد |
| `pytest-cov` | گزارش پوشش کد |

تنظیمات در `pytest.ini` است: `asyncio_mode = auto`، `timeout = 30`، مسیر `tests`.

## ۲) اجرای تست‌ها

```bat
:: همه‌ی تست‌ها، با جزئیات
.venv\Scripts\python.exe -m pytest -v

:: همه به‌جز تست‌هایی که به mpv واقعی نیاز دارند (پیش‌فرض CI)
.venv\Scripts\python.exe -m pytest -v -m "not real_mpv"

:: فقط تست‌های mpv واقعی (روی سیستمی که mpv نصب دارد)
.venv\Scripts\python.exe -m pytest -v -m real_mpv

:: یک فایل یا یک تست خاص
.venv\Scripts\python.exe -m pytest -v tests\test_resync.py
.venv\Scripts\python.exe -m pytest -v tests\test_countdown.py::test_pause_during_the_countdown_cancels_it_everywhere

:: گزارش پوشش کد در ترمینال
.venv\Scripts\python.exe -m pytest --cov=lanwatch --cov-report=term-missing

:: گزارش HTML (فایل htmlcov\index.html)
.venv\Scripts\python.exe -m pytest --cov=lanwatch --cov-report=html
```

نکته: تست‌های `real_mpv` به‌طور پیش‌فرض با مارکر کنار گذاشته می‌شوند و اگر mpv نصب
نباشد خودشان `skip` می‌شوند؛ بنابراین اجرای ساده‌ی `pytest` روی هر سیستمی بی‌خطر است.

## ۳) ساختار تست‌ها

| فایل | چه چیزی را می‌سنجد |
|---|---|
| `tests/conftest.py` | مسیر ماژول‌ها، فیکسچرهای `host_node`، `client_node`، `three_peers`، `settings_file` و کم‌کردن تعداد دورهای همگام‌سازی ساعت برای سرعت |
| `tests/helpers.py` | `FakePlayer` (جانشین mpv)، `EventCollector` (ثبت رویدادها)، `RawPeer` (کلاینت خام WebSocket برای دیدن پیام‌های خام میزبان)، `wait_until`، `start_peer` |
| `tests/test_protocol.py` | ساخت پاکت پیام (uuid4 یکتا، زمان یکنوا)، رفت‌وبرگشت JSON برای همه‌ی پیام‌ها، `MessageTracker` و حذف اکو، ریاضیات اختلاف ساعت با مقادیر تصادفی (خطای < ۵۰ میلی‌ثانیه) |
| `tests/test_clock_sync.py` | تخمین اختلاف ساعت با تأخیر تصادفی ۰ تا ۵۰ میلی‌ثانیه (خطای < ۱۰ میلی‌ثانیه)، حذف یک نمونه‌ی پرت ۵۰۰ میلی‌ثانیه‌ای توسط میانه، پاسخ `answer_ping` |
| `tests/test_player.py` | حالت معلق قبل از بارگذاری فایل، `seek absolute+exact`، نبودِ micro-seek زیر آستانه، `get_position`/`get_duration`، بستن تکراری و ایمن، و `FakePlayer` |
| `tests/test_host_client.py` | میزبان + کلاینت‌های واقعی: پخش/توقف/جست‌وجو/Stop، حذف اکو، ورود دیرهنگام (در حال پخش و متوقف)، هشدار اختلاف طول ویدیو، `peer_left`، مرگ میزبان، ۵ کلاینت هم‌زمان (زیر ۲۰۰ میلی‌ثانیه)، ۲۰ seek سریع، Pause+Play فوری، Seek در حالت توقف، JSON خراب، و حذف کلاینت ساکت (heartbeat timeout) |
| `tests/test_resync.py` | Resync از سمت کلاینت در حالت توقف (۴۵ ثانیه)، Resync از سمت میزبان در حال پخش (۲۰ ثانیه)، دو Resync هم‌زمان بدون نوسان |
| `tests/test_countdown.py` | انتقال `start_in_seconds` با msg_id تازه، پخش فوری با مقدار ۰، رسیدن به GO با پخش هم‌زمان، لغو با Pause، لغو با Resync، و بی‌اثر بودن Play در حال پخش |
| `tests/test_discovery.py` | پاک‌سازی نام سرویس، حذف آدرس‌های loopback، معرفی و کشف سرویس mDNS و حذف آن (در صورت نبود zeroconf یا شبکه‌ی مناسب `skip`) |
| `tests/test_gui_smoke.py` | وجود و وضعیت اولیه‌ی ویجت‌ها، ساخت/حذف کارت شمارش معکوس، اتصال دکمه‌ی Resync به کنترلر، ذخیره‌شدن تیک «Countdown before play»، پر شدن Combobox میزبان‌ها، Start Hosting با میزبان ساختگی، و جابه‌جایی Host/Join (بدون تست ظاهر) |
| `tests/test_real_mpv.py` | فقط با `-m real_mpv` و وجود mpv: راه‌اندازی، حالت idle و بستن ایمن |

## ۴) فیکسچرها

- `host_node` — یک میزبان واقعی روی `127.0.0.1` با پورت تصادفی (`port=0`)، `advertise=False`
  (بدون mDNS) و `FakePlayer` با طول ۶۰۰ ثانیه.
- `client_node` — یک کلاینت متصل به همان میزبان.
- `three_peers` — میزبان + دو کلاینت؛ همان «سه بیننده» که تست‌های Resync/Countdown لازم دارند.
- `settings_file` — فایل تنظیمات موقت با `LANWATCH_SETTINGS` (تست‌ها به
  `%APPDATA%` واقعی شما دست نمی‌زنند).
- `fast_handshake` (autouse) — تعداد دورهای همگام‌سازی ساعت را از ۵ به ۲ کم می‌کند تا
  کل مجموعه در چند ثانیه تمام شود. رفتار برنامه تغییر نمی‌کند.

## ۵) تست روی سیستم بدون نمایشگر

تست‌های GUI به Tk و یک نمایشگر نیاز دارند. روی سرور لینوکسی بدون X، کل فایل
`tests/test_gui_smoke.py` به‌صورت `skip` گزارش می‌شود (نه شکست) و بقیه‌ی تست‌ها
کامل اجرا می‌شوند. تست‌های mDNS هم اگر `zeroconf` نصب نباشد یا هیچ رابط شبکه‌ی
غیر-loopback وجود نداشته باشد `skip` می‌شوند.

## ۶) اگر تستی شکست خورد

1. با `-v` و `-x` اجرا کنید تا اولین شکست را ببینید:
   `.venv\Scripts\python.exe -m pytest -v -x`
2. اگر تست شبکه‌ای شکست خورد، مطمئن شوید پورت‌ها توسط فایروال یا برنامه‌ی دیگری
   گرفته نشده‌اند؛ تست‌ها پورت تصادفی می‌گیرند، پس معمولاً مشکل از این نیست.
3. تست‌های زمان‌محور (شمارش معکوس) از `wait_until` با مهلت استفاده می‌کنند؛ روی
   ماشین خیلی کند ممکن است مهلت‌ها را کمی زیاد کنید — ولی به‌جای شل‌کردن تست،
   ابتدا علت را پیدا کنید.
