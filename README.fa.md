# GOST Manager

GOST Manager یک ابزار Bash منومحور برای نصب GOST v3 و مدیریت چند پروفایل
شماره‌دار Direct Mode در سمت ایران و خارج روی Ubuntu 22.04 و Ubuntu 24.04 است.

Direct Mode تنها حالت ترافیکی پشتیبانی‌شده در GOST Manager v2.0.1 است. پروژه artifact رسمی
go-gost/gost را بدون تغییر نصب می‌کند و فقط wrapper نصب، پیکربندی و سرویس است؛
رفتار protocol در GOST upstream را تغییر نمی‌دهد.

NGINX Gateway و Native GOST Gateway لغو شده‌اند. هیچ placeholder، فرمان مخفی،
route runtime، controller، failover یا وابستگی NGINX وجود ندارد. مدیریت کامل
چند پروفایل مستقل Direct Mode شامل ساخت، جزئیات، ویرایش، clone، restart و حذف
امن است.

## قابلیت‌ها

- نصب یا به‌روزرسانی artifact رسمی GOST؛
- ساخت چند تونل مستقل Kharej؛
- ساخت چند تونل مستقل Iran؛
- حذف، status، log و restart هر تونل به‌صورت مستقل؛
- پاک‌سازی امن فایل‌های خراب یا قدیمی مدیریت‌شده؛
- Monitoring Lite محلی و اختیاری؛
- حذف امن و component-aware.

## نصب و اجرا

روش اصلی نصب، اجرای `setup.sh` از یک shell با دسترسی root است. این فرمان آخرین
GitHub Release پایدار را انتخاب می‌کند، SHA256 و ساختار archive را بررسی
می‌کند، نصب‌کننده محلی transactional را اجرا می‌کند و source تأییدشده را در
`/opt/GOST-Manager` نگه می‌دارد:

    bash <(curl -fsSL https://raw.githubusercontent.com/WikiPanel/GOST-Manager/main/setup.sh)

این حالت پیش‌فرض معادل انتخاب `GOST_MANAGER_VERSION=latest` است.
Setup هم فایل عادی `/etc/os-release` و هم symlink استاندارد Ubuntu به
`/usr/lib/os-release` را می‌پذیرد؛ metadata فقط در همین مسیرهای مورد اعتماد
resolve و پیش از هر دانلود به‌صورت داده parse می‌شود.

برای نصب نسخه دقیق، هر دو فرم `v2.0.1` و `2.0.1` پذیرفته می‌شوند:

    GOST_MANAGER_VERSION=v2.0.1 \
    bash <(curl -fsSL https://raw.githubusercontent.com/WikiPanel/GOST-Manager/main/setup.sh)

برای upgrade همان فرمان latest را دوباره اجرا کنید. نصب مجدد همان نسخه
idempotent است. نسخه و منو با این دستورها در دسترس‌اند:

    gost-manager --version
    gost-manager

`setup.sh` فقط downloader و selector آنلاین Release، بررسی‌کننده Checksum و
archive، و به‌روزرسان امن `/opt/GOST-Manager` است. `install.sh` نصب‌کننده محلی
داخل Release تأییدشده است. فایل خام setup کل application payload نیست؛ archive
کامل پیش از اجرای محتوای دانلودشده روی disk ذخیره و با SHA256 تأیید می‌شود.
منبع اعتماد، GitHub Releases رسمی `WikiPanel/GOST-Manager` روی HTTPS است.

نصب دستی با Git به‌عنوان fallback باقی می‌ماند:

    apt-get update
    apt-get install -y git ca-certificates curl
    git clone --depth 1 https://github.com/WikiPanel/GOST-Manager.git /opt/GOST-Manager
    cd /opt/GOST-Manager
    bash install.sh --install-dependencies
    gost-manager

اجرای مستقیم از مخزن نیز ممکن است:

    sudo bash gost-manager.sh --version
    sudo bash gost-manager.sh

نصب‌کننده باینری رسمی GOST، runnerهای Direct Mode، manager و Monitoring Lite
را نصب می‌کند. در upgrade، فایل‌های موجود /etc/gost/iran-*.env و
/etc/gost/kharej-*.env، unitهای تونل، mode فایل‌ها و وضعیت active سرویس‌های
Direct Mode حفظ می‌شوند. مسیرهای `/etc/gost/`، `/etc/gost-manager/`،
`/var/lib/gost-manager/`، unit و drop-inهای شماره‌دار Iran/Kharej و
`/etc/sysctl.d/99-gost-stability.conf` تغییر ناخواسته نمی‌کنند. فایل VERSION
در `/usr/local/lib/gost-manager/VERSION` نصب می‌شود و monitoring database حفظ
می‌ماند.

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

    GOST Manager v2.0.1
    ====================

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
    11) Server Stability
    0) Exit

گزینه‌های ۱ تا ۱۰ شماره و رفتار فعلی خود را حفظ کرده‌اند. گزینه ۱۱ فقط Wizard
عملیاتی Server Stability است؛ گزینه ۱۲ و placeholder مربوط به Native Gateway
وجود ندارد.

## ساخت تونل Kharej

گزینه ۲ را انتخاب کنید. نمونه:

    Kharej profile number [1]:
    Profile label (optional): kharej-edge
    SOCKS listen port: 28420
    GOST username: maya
    GOST password: ورودی مخفی و دارای تأیید
    Allowed Iran IPv4/CIDRs: 198.51.100.10,198.51.100.11/32
    Apply profile-scoped iptables firewall rules? yes

فایل‌های مدیریت‌شده:

    /etc/gost/kharej-1.env
    /etc/systemd/system/gost-kharej-1.service

## ساخت تونل Iran

گزینه ۳ را انتخاب کنید. نمونه:

    Iran profile number [1]:
    Profile label (optional): iran-edge
    Kharej IP: 203.0.113.20
    Kharej SOCKS port: 28420
    GOST username: maya
    GOST password: مقدار یکسان به‌صورت ورودی مخفی
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

گزینه ۸ ابتدا فهرست همه پروفایل‌ها را نشان می‌دهد و سپس زیرمنوی زیر را باز
می‌کند:

    Direct Mode profiles
    ====================

    1) List all profiles
    2) Show profile detail
    3) Edit a profile
    4) Clone a profile
    5) Restart selected profiles
    6) Restart all profiles
    0) Back

فضای شماره Iran و Kharej مستقل است و اولین شکاف آزاد با بررسی هم‌زمان env و
unit پیشنهاد می‌شود؛ orphan هرگز overwrite نمی‌شود. `PROFILE_LABEL` فقط
metadata نمایشی است و شناسه، نام فایل و سرویس همچنان `side-number` می‌ماند.
پروفایل‌های قدیمی بدون label معتبر هستند.

پورت محلی در هر دو سمت به‌صورت سراسری و با یک snapshot زنده `ss` بررسی
می‌شود. ویرایش credential موجود و کلیدهای ناشناخته معتبر را حفظ می‌کند، diff
بدون secret نشان می‌دهد و برای no-op هیچ write یا restart ندارد. clone منبع
را تغییر نمی‌دهد. restart گروهی فقط شناسه‌های دقیق comma-separated را می‌پذیرد
و از wildcard استفاده نمی‌کند.

## Server Stability

گزینه ۱۱ وضعیت فعلی kernel و سرویس‌های دقیق `gost-iran-N.service` و
`gost-kharej-N.service` را بررسی می‌کند. سپس فایل مدیریت‌شده
`/etc/sysctl.d/99-gost-stability.conf` و drop-in مستقل
`/etc/systemd/system/<service>.d/stability.conf` را در صورت نیاز به‌صورت امن
می‌نویسد، `sysctl --system` را اجرا می‌کند و تمام مقدارها را دوباره بررسی
می‌کند.

این Wizard هیچ سرویس GOST را restart، stop یا start نمی‌کند. اگر drop-in جدیدی
ساخته شود فقط یک `systemctl daemon-reload` اجرا می‌شود و limitهای جدید پس از
restart عادی بعدی همان سرویس اعمال می‌شوند. اجرای دوباره idempotent است و
فایل یا reload غیرضروری ایجاد نمی‌کند. symlink یا فایل نامرتبط overwrite
نمی‌شود و env، unit اصلی، runner، باینری GOST و schema مانیتورینگ تغییر
نمی‌کنند.

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

داده‌ی conntrack نیز اختیاری است. اگر هر دو فایل sysctl مربوط به conntrack روی
میزبان وجود نداشته باشند، خروجی `conntrack: unsupported` نمایش داده می‌شود؛
این حالت شمارنده‌ی خطا را افزایش نمی‌دهد و سلامت سایر مشاهده‌های معتبر را
کاهش نمی‌دهد. وجود ناقص فایل‌ها، خطای مجوز یا مقدار نامعتبر همچنان failure
واقعی است. collector برای تغییر قابلیت kernel هیچ moduleای load نمی‌کند.

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

پورت SOCKS سمت Kharej باید فقط برای sourceهای IPv4 سمت Iran قابل دسترس باشد.
`ALLOWED_IRAN_SOURCES` حداکثر ۶۴ شبکه canonical از `/8` تا `/32` را می‌پذیرد؛
آدرس ساده به `/32` تبدیل می‌شود. `IRAN_IP` قدیمی همچنان معتبر است و هنگام
list یا upgrade بازنویسی نمی‌شود. برای هر source یک ACCEPT پیش از DROP نهایی
همان پروفایل ساخته می‌شود. commentها مخصوص همان profile number هستند و
rollback یا پاک‌سازی به rule نامرتبط یا پروفایل دیگر دست نمی‌زند.

password، IP واقعی، token، UUID، private key یا credential را commit نکنید.
secretها فقط در فایل‌های root-owned با mode برابر 0600 زیر /etc/gost باقی
می‌مانند. password به‌صورت مخفی و با تأیید دریافت می‌شود و در list، detail،
status، summary یا failure چاپ نمی‌شود. جایگزینی env به‌صورت atomic و با فایل
موقت خصوصی در همان directory است. Monitoring فقط فیلدهای topology معتبر را می‌خواند و credential را
در metric، event، metadata، state یا export ذخیره نمی‌کند.

## توسعه و اعتبارسنجی

    make check

تست‌ها بدون root و با temporary directory و command stub اجرا می‌شوند. ماتریس
Ubuntu 22.04 و Ubuntu 24.04 نیز gate کامل Direct Mode و Monitoring Lite را
اجرا می‌کند.
