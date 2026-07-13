# GOST Manager

GOST Manager یک ابزار Bash منومحور برای نصب GOST v3 و مدیریت چند پروفایل
شماره‌دار Direct Mode در سمت ایران و خارج روی Ubuntu 22.04 و Ubuntu 24.04 است.

Direct Mode تنها حالت ترافیکی پشتیبانی‌شده در v0.2 است. پروژه artifact رسمی
go-gost/gost را بدون تغییر نصب می‌کند و فقط wrapper نصب، پیکربندی و سرویس است؛
رفتار protocol در GOST upstream را تغییر نمی‌دهد.

NGINX Gateway و Native GOST Gateway لغو شده‌اند. هیچ placeholder، فرمان مخفی،
route runtime یا وابستگی NGINX وجود ندارد. Issue #29 برای بهبود مدیریت چند
سرور و چند پروفایل Direct Mode در آینده در نظر گرفته شده و بخشی از این نسخه
نیست.

## قابلیت‌ها

- نصب یا به‌روزرسانی artifact رسمی GOST؛
- ساخت چند تونل مستقل Kharej؛
- ساخت چند تونل مستقل Iran؛
- حذف، status، log و restart هر تونل به‌صورت مستقل؛
- پاک‌سازی امن فایل‌های خراب یا قدیمی مدیریت‌شده؛
- Monitoring Lite محلی و اختیاری؛
- حذف امن و component-aware.

## نصب و اجرا

    sudo bash install.sh
    sudo gost-manager

اجرای مستقیم از مخزن نیز ممکن است:

    sudo bash gost-manager.sh

نصب‌کننده باینری رسمی GOST، runnerهای Direct Mode، manager و Monitoring Lite
را نصب می‌کند. در upgrade، فایل‌های موجود /etc/gost/iran-*.env و
/etc/gost/kharej-*.env، unitهای تونل، mode فایل‌ها و وضعیت active سرویس‌های
Direct Mode حفظ می‌شوند.

## مسیر ترافیک

سمت خارج یک SOCKS5 با GOST اجرا می‌کند. سمت ایران مستقیماً روی پورت عمومی
گوش می‌دهد و ترافیک را از طریق SOCKS به پورت محلی مقصد روی سرور خارج
می‌فرستد:

    Client
      -> Iran GOST public listener
      -> Kharej GOST SOCKS5
      -> 127.0.0.1:<target-port>

در این مسیر NGINX وجود ندارد.

## منوی اصلی

    GOST Manager
    ============

    1) Install / Update GOST
    2) Create Kharej tunnel
    3) Create Iran tunnel
    4) Delete tunnel
    5) Show status
    6) Show logs
    7) Restart tunnel
    8) List active GOST services
    9) Clean old/broken GOST configs
    10) Monitoring
    0) Exit

گزینه‌های ۱ تا ۱۰ شماره و رفتار فعلی خود را حفظ کرده‌اند. گزینه ۱۱ یا ۱۲ و
placeholder مربوط به Native Gateway وجود ندارد.

## ساخت تونل Kharej

گزینه ۲ را انتخاب کنید. نمونه:

    Tunnel number: 1
    SOCKS listen port: 28420
    GOST username: maya
    GOST password: برای ساخت خودکار خالی بگذارید
    Iran IP allowed: YOUR_IRAN_SERVER_IP
    Apply iptables firewall rule? yes

فایل‌های مدیریت‌شده:

    /etc/gost/kharej-1.env
    /etc/systemd/system/gost-kharej-1.service

## ساخت تونل Iran

گزینه ۳ را انتخاب کنید. نمونه:

    Tunnel number: 1
    Kharej IP: YOUR_KHAREJ_SERVER_IP
    Kharej SOCKS port: 28420
    GOST username: maya
    GOST password: مقدار ساخته‌شده در Kharej
    Port mappings: 2052:2052

برای چند پورت:

    80:80,8080:8080,8880:8880

Port mappings اجباری است. فرمت اشتباه، پورت خارج از محدوده، listen port
تکراری یا پورت مشغول پیش از نوشتن فایل رد می‌شود.

فایل‌های مدیریت‌شده:

    /etc/gost/iran-1.env
    /etc/systemd/system/gost-iran-1.service

## مدیریت تونل

گزینه‌های delete، status، logs و restart فهرست شماره‌دار تونل‌های Iran و
Kharej را نشان می‌دهند. هر پروفایل مستقل است؛ حذف iran-2 روی iran-1 یا
kharej-1 اثری ندارد.

## Monitoring Lite

گزینه ۱۰ مانیتورینگ محلی را باز می‌کند:

    1) Live resources
    2) Last 10 minutes
    3) Last 30 minutes
    4) Last 1 hour
    5) Services and tunnels
    6) Collector status
    7) Advanced tools
    0) Back

بخش‌های عادی آن عبارت‌اند از:

- HOST؛
- NETWORK با loopback جدا از external؛
- TCP / CONNECTIONS؛
- همه سرویس‌های Direct Mode GOST؛
- همه تونل‌های فعلی Iran و Kharej؛
- COLLECTOR.

Monitoring Lite اختیاری است، به NGINX وابسته نیست و در مسیر ترافیک قرار
نمی‌گیرد. failure، maintenance یا حذف آن هیچ سرویس GOST را stop، restart یا
reconfigure نمی‌کند.

دستورات مستقیم:

    gost-monitor snapshot
    gost-monitor live
    gost-monitor summary --window 10m
    gost-monitor summary --window 30m
    gost-monitor summary --window 1h
    gost-monitor-admin status
    gost-monitor-admin maintenance

SQLite با schema version 4، WAL و retention محدود حفظ شده است:

- raw metrics: شش ساعت؛
- minute rollups: بیست‌وچهار ساعت؛
- structured events: بیست‌وچهار ساعت.

برای پروفایل نماینده شامل شش سرویس GOST، برآورد محافظه‌کارانه کل database
حدود 0.451 GiB است؛ حداقل 1 GiB فضا رزرو کنید. history موجود migrate یا پاک
نمی‌شود و rowهای generic قدیمی با retention عادی منقضی می‌شوند.

fixture قطعی ۱٬۰۰۰ کاربر سربار parsing، attribution، storage و scheduling را
می‌سنجد و اثبات ظرفیت شبکه نیست. ظرفیت واقعی به CPU، kernel، NIC، encryption،
GOST، RTT، CDN و ارائه‌دهنده بستگی دارد.

## حذف امن

    sudo bash uninstall.sh

حذف manager، سرویس Monitoring، کد Monitoring، config، history، سرویس‌های
Direct Mode، فایل‌های /etc/gost و باینری رسمی GOST تأییدهای مستقل با پیش‌فرض
No دارند. plan نهایی پیش از اجرا نمایش داده می‌شود.

اگر حتی یک unit دقیق مدیریت‌شده باقی بماند، هر دو runner، /etc/gost و باینری
GOST حفظ می‌شوند. unmanaged unitها و ruleهای firewall نامرتبط دست‌نخورده
می‌مانند. حذف فقط Monitoring هیچ تغییری در ترافیک Direct Mode ایجاد نمی‌کند.

## امنیت

پورت SOCKS سمت Kharej باید فقط برای IP سمت Iran قابل دسترس باشد. ruleهای
iptables ایجادشده comment مخصوص همان tunnel number دارند و پاک‌سازی فقط همان
ruleها را هدف می‌گیرد.

password، IP واقعی، token، UUID، private key یا credential را commit نکنید.
secretها فقط در فایل‌های root-owned با mode برابر 0600 زیر /etc/gost باقی
می‌مانند. Monitoring فقط فیلدهای topology معتبر را می‌خواند و credential را
در metric، event، metadata، state یا export ذخیره نمی‌کند.

## توسعه و اعتبارسنجی

    make check

تست‌ها بدون root و با temporary directory و command stub اجرا می‌شوند. ماتریس
Ubuntu 22.04 و Ubuntu 24.04 نیز gate کامل Direct Mode و Monitoring Lite را
اجرا می‌کند.
