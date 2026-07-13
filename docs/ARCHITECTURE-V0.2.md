# GOST Manager v0.2 Architecture

## Product boundary

GOST Manager v0.2 supports one traffic architecture: official upstream GOST
in Direct Mode. NGINX Gateway and Native GOST Gateway are cancelled. The
project contains no Gateway placeholder, hidden command, desired-state layer,
route runtime, or NGINX dependency.

The supported product consists of:

- multiple independent Iran Direct Mode profiles;
- multiple independent Kharej Direct Mode profiles;
- installation of the official go-gost/gost release artifact unchanged;
- local systemd service management;
- optional local Monitoring Lite;
- safe, component-aware uninstall.

Multi-server management is implemented as operations on independent Direct
Mode profiles. It does not add a shared process, controller, synchronization,
failover, or connection-migration layer.

## Traffic path

Each numbered profile is independent:

    Client
      |
    Iran public GOST listener
      | SOCKS5
    Kharej GOST listener
      |
    127.0.0.1:<target-port>

The manager keeps the established Direct Mode contract:

- Iran configuration: /etc/gost/iran-<number>.env;
- Kharej configuration: /etc/gost/kharej-<number>.env;
- Iran unit: gost-iran-<number>.service;
- Kharej unit: gost-kharej-<number>.service;
- Iran runner: /usr/local/lib/gost-manager/gost-run-iran.sh;
- Kharej runner: /usr/local/lib/gost-manager/gost-run-kharej.sh.

Existing env files, unit contents, file modes, and active service states are
preserved during an ordinary manager upgrade.

## Upstream GOST boundary

GOST Manager does not vendor, patch, fork, or rebuild GOST. The installer
selects an architecture-specific official release artifact, verifies the
official checksum when available, and installs the downloaded bytes at
/usr/local/bin/gost with executable mode.

The repository manages configuration and service invocation only. Upstream
GOST protocol behavior is not changed by this project.

## Menu contract

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
    11) Server Stability
    0) Exit

Options 1 through 10 retain their established labels and behavior. Option 11
is an operational stability wizard only. There is no option 12 or
native-gateway placeholder.

Option 8 renders all profiles and opens a profile submenu for list, detail,
edit, clone, restart-selected, and restart-all. Identity is the immutable pair
`side + positive integer`; optional `PROFILE_LABEL` metadata never changes the
env path, unit, process, or connectivity. Iran and Kharej have independent
number spaces and the allocator treats either an env or unit as occupied.

Every profile remains one official GOST process. Local listener ports are
unique across both sides. Candidate changes are validated against configured
profiles and one live socket snapshot. Env files are parsed as strict data,
never sourced, and are atomically replaced from private same-directory files.
Passwords are hidden and retained unless explicitly replaced.

## Monitoring boundary

Monitoring Lite is local, optional, and outside the traffic path. It observes:

- host CPU, load, memory, swap, filesystems, and storage;
- external and loopback network counters;
- TCP states, retransmits, listen drops, and conntrack where available;
- all current Direct Mode GOST services;
- all current Iran and Kharej profiles;
- collector timing, errors, and database size.

The supported operator views are live, 10 minutes, 30 minutes, and 1 hour.
Monitoring uses Python 3 standard library modules and a local SQLite database
with WAL, bounded retention, and concurrent read support. It does not discover
or require NGINX and cannot start, stop, restart, reload, or reconfigure a
traffic service.

Historical generic rows already present in a monitoring database remain
subject to the existing retention policy. This scope reset does not change the
schema version and does not erase history.

## Managed paths

    /etc/gost/
      iran-<number>.env
      kharej-<number>.env

    /etc/gost-manager/
      monitoring.env

    /var/lib/gost-manager/
      metrics.sqlite3

    /usr/local/lib/gost-manager/
      gost-run-iran.sh
      gost-run-kharej.sh
      monitoring/

Monitoring state is separate from Direct Mode configuration. Removing
Monitoring Lite does not remove or alter tunnel env files, units, runners, or
the official GOST binary.

The optional Server Stability wizard owns only:

    /etc/sysctl.d/99-gost-stability.conf
    /etc/systemd/system/gost-<side>-<number>.service.d/stability.conf

It matches only exact numbered Direct Mode units, writes no desired traffic
state, and never starts, stops, or restarts a GOST service. Kernel settings are
verified live after `sysctl --system`; systemd drop-ins take effect after the
operator's next normal service restart. Monitoring and traffic remain
independent of this helper.

## Failure and rollback

- A monitoring failure never changes the traffic path.
- Installer file replacement is staged and rolled back on validation failure.
- Existing collector active/enabled state is restored on failed upgrade.
- Uninstall uses exact managed service matching and independent confirmations.
- Surviving Direct Mode units preserve both runners, /etc/gost, and the GOST
  binary.
- Unmanaged systemd units, firewall rules, and host services remain untouched.
- Create and clone rollback only the new identity. Edit restores exact prior
  env bytes, mode, profile-scoped firewall rules, and service state on failure.
- Delete preserves env, unit, and firewall whenever exact stop/disable fails.
- Kharej firewall rules use one ACCEPT per canonical allowed source followed
  by one exact profile DROP; rollback preserves rule order and other profiles.

## Security

Real addresses, usernames, passwords, tokens, UUIDs, and private keys are never
committed. Direct Mode secrets remain in root-owned 0600 env files.
Monitoring reads only validated topology fields and never stores credentials
in metrics, events, entity metadata, state, exports, or test fixtures.
