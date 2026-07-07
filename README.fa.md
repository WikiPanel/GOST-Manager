# GOST Manager

مدیر GOST یک ابزار Bash منومحور برای نصب GOST v3 و مدیریت تونل‌های شماره‌دار سمت ایران و خارج روی Ubuntu 22.04 و Ubuntu 24.04 است.

این پروژه فقط یک اسکریپت تکی نیست؛ شامل نصب‌کننده، حذف‌کننده، runner های systemd، نمونه env، مستندات، تست و GitHub Actions است.

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

سپس سمت و شماره تونل را وارد کنید:

```text
Tunnel side: iran
Tunnel number: 1
```

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
