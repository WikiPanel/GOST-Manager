# GOST Manager

مدیر GOST یک ابزار Bash منومحور برای نصب GOST v3 و مدیریت تونل‌های شماره‌دار سمت ایران و خارج روی Ubuntu 22.04 و Ubuntu 24.04 است.

این پروژه فقط یک اسکریپت تکی نیست؛ شامل نصب‌کننده، حذف‌کننده، runner های systemd، نمونه env، مستندات و تست محلی است.

## معرفی پروژه

با این ابزار می‌توانید:

- GOST v3 را از Release رسمی `go-gost/gost` نصب یا به‌روزرسانی کنید
- تونل شماره‌دار سمت خارج بسازید
- تونل شماره‌دار سمت ایران بسازید
- تونل‌ها را حذف، ری‌استارت، بررسی و لاگ‌گیری کنید
- سرویس‌های فعال GOST را ببینید
- فایل‌های خراب یا قدیمی مربوط به همین پروژه را با تایید دستی پاک کنید

بعد از نصب:

```bash
sudo gost-manager
```

نصب‌کننده علاوه بر manager و runnerهای قبلی، کل پکیج مانیتورینگ را در `/usr/local/lib/gost-manager/monitoring`، سه launcher را در `/usr/local/sbin`، تنظیمات را در `/etc/gost-manager/monitoring.env`، unit را در `/etc/systemd/system/gost-monitor-collector.service` و تاریخچه را در `/var/lib/gost-manager/metrics.sqlite3` نصب می‌کند.

در نصب تازه collector فعال و اجرا می‌شود. در ارتقا، config معتبر سفارشی، تاریخچه، و وضعیت enabled/active قبلی collector حفظ می‌شود. فایل‌های `/etc/gost/iran-*.env` و `/etc/gost/kharej-*.env`، unitهای تونل و وضعیت ترافیک بدون تغییر باقی می‌مانند.

اجرای مستقیم هم ممکن است:

```bash
sudo bash gost-manager.sh
```

## سناریوی ایران/خارج

سمت خارج یک SOCKS5 با GOST اجرا می‌شود. سمت ایران روی پورت‌های عمومی گوش می‌دهد و ترافیک را از طریق SOCKS سمت خارج به `127.0.0.1:<target_port>` روی همان سرور خارج می‌فرستد.

در این مسیر Nginx استفاده نمی‌شود. خود GOST مستقیم روی پورت عمومی ایران listen می‌کند.

## ساخت تونل سمت خارج

در سرور خارج اجرا کنید:

```bash
sudo gost-manager
```

گزینه زیر را انتخاب کنید:

```text
2) Create Kharej tunnel
```

نمونه ورودی:

```text
Tunnel number: 1
SOCKS listen port: 28420
GOST username: maya
GOST password: خالی بگذارید تا ساخته شود
Iran IP allowed: YOUR_IRAN_SERVER_IP
Apply iptables firewall rule? yes
```

فایل‌ها:

```text
/etc/gost/kharej-1.env
/etc/systemd/system/gost-kharej-1.service
```

## ساخت تونل سمت ایران

در سرور ایران اجرا کنید:

```bash
sudo gost-manager
```

گزینه زیر را انتخاب کنید:

```text
3) Create Iran tunnel
```

نمونه ورودی:

```text
Tunnel number: 1
Kharej IP: YOUR_KHAREJ_SERVER_IP
Kharej SOCKS port: 28420
GOST username: maya
GOST password: پسورد ساخته‌شده در سمت خارج
Port mappings: 2052:2052
```

ورودی `Port mappings` برای هر تونل سمت ایران اجباری است. مقدار خالی، فرمت اشتباه، پورت نامعتبر و پورت listen تکراری قبل از ساخت فایل‌ها رد می‌شود.

## مثال پورت 2052

```text
Iran :2052
-> gost-iran-1
-> Kharej :28420 SOCKS5
-> Kharej 127.0.0.1:2052
```

## مثال پورت‌های 80/8080/8880

برای چند پورت:

```text
80:80,8080:8080,8880:8880
```

نتیجه:

```text
Iran :80   -> Kharej 127.0.0.1:80
Iran :8080 -> Kharej 127.0.0.1:8080
Iran :8880 -> Kharej 127.0.0.1:8880
```

## مشاهده وضعیت

گزینه:

```text
5) Show status
```

برای مشاهده وضعیت، لاگ، ری‌استارت و حذف، برنامه یک فهرست شماره‌دار از تونل‌های موجود نشان می‌دهد. دیگر لازم نیست `iran` یا `kharej` را دستی تایپ کنید.

```text
Available GOST tunnels:

1) gost-iran-1.service      active/running    /etc/gost/iran-1.env
2) gost-kharej-1.service    active/running    /etc/gost/kharej-1.env

Select tunnel number:
```

## مانیتورینگ محلی

گزینه‌های جدید منوی اصلی بدون تغییر شماره‌های ۱ تا ۹ اضافه شده‌اند:

```text
10) Monitoring
11) Native GOST Gateway (Coming soon)
```

زیرمنوی Monitoring شامل snapshot، داشبورد live، خلاصه‌های ۱۰ دقیقه، ۳۰ دقیقه، ۱ ساعت و بازه سفارشی، جزئیات host/network/service/tunnel/collector، eventها، خروجی JSON/CSV، کنترل collector، اجرای one-shot، maintenance و حذف صریح تاریخچه است. خطای مانیتورینگ از منو خارج نمی‌شود و هیچ سرویس ترافیکی را تغییر نمی‌دهد.

دستورات مستقیم:

```bash
systemctl status gost-monitor-collector.service
systemctl start gost-monitor-collector.service
systemctl stop gost-monitor-collector.service
systemctl restart gost-monitor-collector.service

gost-monitor snapshot
gost-monitor live
gost-monitor summary --window 10m
gost-monitor-admin status
gost-monitor-admin maintenance
```

فایل تنظیمات با مالک `root:root` و mode برابر `0600` فقط کلیدهای زیر را می‌پذیرد:

```text
GOST_MONITOR_DB=/var/lib/gost-manager/metrics.sqlite3
GOST_ENV_DIR=/etc/gost
GOST_MONITOR_SAMPLE_INTERVAL=5
GOST_MONITOR_TCP_INTERVAL=30
GOST_MONITOR_SLOW_INTERVAL=60
GOST_MONITOR_MAINTENANCE_INTERVAL=900
```

محدوده sample برابر ۵ تا ۶۰ ثانیه است؛ TCP برابر ۱۰ تا ۳۰۰ و حداقل sample؛ slow برابر ۳۰ تا ۹۰۰ و حداقل sample؛ maintenance برابر ۳۰۰ تا ۸۶۴۰۰ و حداقل slow. این فایل هرگز در Bash source یا اجرا نمی‌شود. کلید ناشناخته/تکراری، مسیر نسبی، substitution شل، quoting ناامن و cadence نامعتبر رد می‌شود.

نگه‌داری پیش‌فرض شامل ۴۸ ساعت raw metric، سی روز minute rollup و سی روز event ساختاریافته است. برای پروفایل نمونه حداقل ۱۲ GiB فضا در نظر بگیرید. Maintenance در یک transaction انجام می‌شود و checkpoint بعد از commit است. حذف تاریخچه در منو نیازمند عبارت دقیق `DELETE MONITORING HISTORY` است، فقط collector را در صورت نیاز متوقف می‌کند، DB را اتمیک جایگزین می‌کند و فقط اگر collector قبلاً active بوده آن را دوباره اجرا می‌کند. ترافیک و `/etc/gost` دست‌نخورده می‌مانند.

سرویس collector restart محدود، اولویت CPU/I/O پایین، UMask خصوصی و state mode برابر 0700 دارد و هیچ وابستگی یا hook توقف/restart/reload برای NGINX یا سرویس‌های GOST ندارد. خرابی collector، DB، maintenance یا حذف مانیتورینگ باعث توقف Direct Mode نمی‌شود.

گزینه Native GOST Gateway فقط پیام `Coming soon` چاپ می‌کند و هیچ package، فایل، directory، service، database، firewall، NGINX یا GOST را تغییر نمی‌دهد.

## حذف امن

`sudo bash uninstall.sh` را اجرا کنید. حذف manager، سرویس مانیتورینگ، کد مانیتورینگ، config، history، سرویس‌های ترافیکی، credentialهای `/etc/gost` و باینری GOST هرکدام تأیید مستقل دارند و پیش‌فرض همه No است. قبل از اجرا plan نهایی نمایش داده می‌شود.

حذف فقط مانیتورینگ، unitها و وضعیت فعال تونل‌ها، runnerها، `/etc/gost`، باینری GOST، firewall و NGINX را تغییر نمی‌دهد. config و history انتخاب‌های جدا هستند. تا وقتی collector service باقی است کد/config لازم حذف نمی‌شود و تا وقتی سرویس ترافیکی باقی است runnerها حفظ می‌شوند. اگر config/history را نگه دارید، اجرای دوباره `sudo bash install.sh` مانیتورینگ را بازیابی و DB را validate/migrate می‌کند.

## مشاهده لاگ

گزینه:

```text
6) Show logs
```

این گزینه آخرین لاگ‌های سرویس systemd مربوط به همان تونل را نشان می‌دهد.

## حذف تونل

گزینه:

```text
4) Delete tunnel
```

حذف `iran-2` روی `iran-1` اثری ندارد. حذف `kharej-2` روی `kharej-1` اثری ندارد.

اگر تونل سمت خارج rule های iptables داشته باشد، فقط rule هایی حذف می‌شوند که comment مربوط به همان شماره تونل را دارند.

## هشدار امنیتی SOCKS سمت خارج

پورت SOCKS سمت خارج نباید عمومی باشد. هنگام ساخت تونل خارج، rule فایروال را فعال کنید تا فقط IP سرور ایران اجازه اتصال داشته باشد.

## هشدار ماندگار نبودن iptables بعد از reboot

Rule های iptables به‌صورت پیش‌فرض بعد از reboot ماندگار نیستند. برای نگه‌داری دائمی، از `netfilter-persistent` یا سیستم فایروال سرور خود استفاده کنید.

## هشدار ذخیره نکردن پسورد واقعی داخل GitHub

پسورد واقعی فقط باید داخل فایل‌های `/etc/gost/*.env` روی سرور باشد. فایل‌های نمونه این مخزن placeholder دارند. پسورد واقعی، IP تولیدی، token یا credential خصوصی را داخل GitHub commit نکنید.
