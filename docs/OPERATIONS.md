# Operations Runbook

## Fresh Server Setup

1. Use Ubuntu 22.04 or Ubuntu 24.04.
2. Clone this repository.
3. Install the manager:

```bash
sudo bash install.sh
```

4. Start the menu:

```bash
sudo gost-manager
```

5. Choose `1) Install / Update GOST`.

## Kharej Setup

On the Kharej server:

```bash
sudo gost-manager
```

Choose `2) Create Kharej tunnel`.

Recommended values:

```text
Tunnel number: 1
SOCKS listen port: 28420
GOST username: maya
GOST password: leave empty to generate
Iran IP allowed: YOUR_IRAN_SERVER_IP
Apply iptables firewall rule? yes
```

Save the generated password outside Git. You need it for the Iran side.

## Iran Setup

On the Iran server:

```bash
sudo gost-manager
```

Choose `3) Create Iran tunnel`.

Single-port example:

```text
Tunnel number: 1
Kharej IP: YOUR_KHAREJ_SERVER_IP
Kharej SOCKS port: 28420
GOST username: maya
GOST password: value_from_kharej
Port mappings: 2052:2052
```

The `Port mappings` prompt is required. Use `Iran listen port:Kharej local target port`.

Multi-port example:

```text
Tunnel number: 2
Kharej IP: YOUR_KHAREJ_SERVER_IP
Kharej SOCKS port: 28420
GOST username: maya
GOST password: value_from_kharej
Port mappings: 80:80,8080:8080,8880:8880
```

## Local Test

On the Iran server:

```bash
curl -v --max-time 10 http://127.0.0.1:2052/
curl -v --max-time 10 http://127.0.0.1:80/
curl -v --max-time 10 http://127.0.0.1:8080/
curl -v --max-time 10 http://127.0.0.1:8880/
```

Use only the ports you mapped.

## Public/CDN Test

From a client or CDN edge path:

```bash
curl -v --max-time 10 http://YOUR_DOMAIN_OR_IP:2052/
curl -v --max-time 10 http://YOUR_DOMAIN_OR_IP:80/
```

## Restart

Use menu option `7) Restart tunnel`. The manager shows a numbered selector, so choose the tunnel from the list instead of typing `iran` or `kharej`.

```text
Available GOST tunnels:

1) gost-iran-1.service      active/running    /etc/gost/iran-1.env
2) gost-kharej-1.service    active/running    /etc/gost/kharej-1.env

Select tunnel number:
```

## Recovery If Service Fails

1. Use menu option `5) Show status` and select the tunnel from the numbered list.
2. Use menu option `6) Show logs` and select the tunnel from the numbered list.
3. Verify `/etc/gost/<side>-<number>.env` exists and is permission `600`.
4. Verify `/usr/local/bin/gost -V` works.
5. On Iran, check whether public listen ports are already owned by another process.
6. On Kharej, check whether firewall rules allow the Iran server IP.
7. Use menu option `9) Clean old/broken GOST configs` only after reviewing the candidate list.
