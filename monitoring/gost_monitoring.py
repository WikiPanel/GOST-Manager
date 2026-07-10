#!/usr/bin/env python3
"""GOST Manager monitoring collector core.

Standard-library only collector for local host/service metrics and legacy
Direct Mode env compatibility.  It never mutates runtime configuration.
"""
from __future__ import annotations

import argparse, dataclasses, json, os, re, shlex, sqlite3, subprocess, time
from pathlib import Path
from typing import Callable, Iterable, Sequence

SCHEMA_VERSION = 2
DEFAULT_DB_PATH = "/var/lib/gost-manager/metrics.sqlite3"
DEFAULT_ENV_DIR = "/etc/gost"
DEFAULT_SAMPLE_INTERVAL_SECONDS = 5.0
RAW_RETENTION_SECONDS = 48 * 3600
ROLLUP_RETENTION_SECONDS = 30 * 24 * 3600
MAINTENANCE_INTERVAL_SECONDS = 15 * 60
QUALITY = ("exact", "derived", "estimated", "unavailable")

SERVICE_RE = re.compile(r"^gost-(iran|kharej)-([1-9][0-9]*)\.service$")
ENV_RE = re.compile(r"^(iran|kharej)-([1-9][0-9]*)\.env$")
LISTEN_RE = re.compile(r"^(?P<proto>\S+)\s+LISTEN\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+(?:\s+users:\(\(\"(?P<process>[^\"]+)\",pid=(?P<pid>\d+),fd=(?P<fd>\d+)\)\))?")

@dataclasses.dataclass(frozen=True)
class Tunnel:
    side: str; number: int; service_name: str; env_path: str
    listen_ports: tuple[int, ...]; target_ports: tuple[int, ...]
    @property
    def tunnel_id(self) -> str: return f"{self.side}-{self.number}"

@dataclasses.dataclass(frozen=True)
class Metric:
    scope: str; name: str; value: float | int | str | None; unit: str; quality: str
    labels: dict[str, str] = dataclasses.field(default_factory=dict)

@dataclasses.dataclass(frozen=True)
class Event:
    ts: int; severity: str; code: str; message: str; details: dict[str, object] = dataclasses.field(default_factory=dict)

@dataclasses.dataclass(frozen=True)
class MetricSample:
    tunnel_id: str | None; collected_at: int; service_state: int; service_substate: int; restart_count: int
    listen_ports_total: int; listen_ports_up: int; configured_mappings_total: int; rx_bytes: int = 0; tx_bytes: int = 0

@dataclasses.dataclass(frozen=True)
class CounterDelta:
    delta: int | None; rate: float | None; quality: str; reset: bool; gap: bool

CREATE_V2 = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tunnels(tunnel_id TEXT PRIMARY KEY, side TEXT NOT NULL CHECK(side IN('iran','kharej')), tunnel_number INTEGER NOT NULL, service_name TEXT NOT NULL UNIQUE, env_path TEXT NOT NULL, listen_ports_json TEXT NOT NULL DEFAULT '[]', target_ports_json TEXT NOT NULL DEFAULT '[]', updated_at INTEGER NOT NULL, UNIQUE(side,tunnel_number));
CREATE TABLE IF NOT EXISTS metric_samples(sample_id INTEGER PRIMARY KEY AUTOINCREMENT, tunnel_id TEXT REFERENCES tunnels(tunnel_id) ON DELETE CASCADE, collected_at INTEGER NOT NULL, service_state INTEGER NOT NULL DEFAULT 0, service_substate INTEGER NOT NULL DEFAULT 0, restart_count INTEGER NOT NULL DEFAULT 0, listen_ports_total INTEGER NOT NULL DEFAULT 0, listen_ports_up INTEGER NOT NULL DEFAULT 0, configured_mappings_total INTEGER NOT NULL DEFAULT 0, rx_bytes INTEGER, tx_bytes INTEGER, UNIQUE(tunnel_id,collected_at));
CREATE INDEX IF NOT EXISTS idx_metric_samples_time ON metric_samples(collected_at);
CREATE TABLE IF NOT EXISTS metrics(sample_id INTEGER NOT NULL REFERENCES metric_samples(sample_id) ON DELETE CASCADE, scope TEXT NOT NULL, name TEXT NOT NULL, value REAL, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), labels_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_metrics_lookup ON metrics(scope,name);
CREATE TABLE IF NOT EXISTS events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, severity TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_events_time ON events(ts);
CREATE TABLE IF NOT EXISTS minute_rollups(scope TEXT NOT NULL, name TEXT NOT NULL, minute_start INTEGER NOT NULL, samples INTEGER NOT NULL, min_value REAL, avg_value REAL, max_value REAL, unavailable_count INTEGER NOT NULL, reset_count INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0, coverage REAL NOT NULL, unit TEXT NOT NULL, quality TEXT NOT NULL, PRIMARY KEY(scope,name,minute_start));
CREATE INDEX IF NOT EXISTS idx_minute_rollups_time ON minute_rollups(minute_start);
CREATE TABLE IF NOT EXISTS collector_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Compatibility schema text retained so tests can create merged PR #7 v1 DBs.
CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tunnels(tunnel_id TEXT PRIMARY KEY, side TEXT NOT NULL, tunnel_number INTEGER NOT NULL, service_name TEXT NOT NULL UNIQUE, env_path TEXT NOT NULL, listen_ports_json TEXT NOT NULL DEFAULT '[]', target_ports_json TEXT NOT NULL DEFAULT '[]', updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS metric_samples(sample_id INTEGER PRIMARY KEY AUTOINCREMENT, tunnel_id TEXT NOT NULL REFERENCES tunnels(tunnel_id) ON DELETE CASCADE, collected_at INTEGER NOT NULL, service_state INTEGER NOT NULL, service_substate INTEGER NOT NULL, restart_count INTEGER NOT NULL DEFAULT 0, listen_ports_total INTEGER NOT NULL DEFAULT 0, listen_ports_up INTEGER NOT NULL DEFAULT 0, configured_mappings_total INTEGER NOT NULL DEFAULT 0, rx_bytes INTEGER NOT NULL DEFAULT 0, tx_bytes INTEGER NOT NULL DEFAULT 0, UNIQUE(tunnel_id,collected_at));
CREATE TABLE IF NOT EXISTS metric_rollups(tunnel_id TEXT NOT NULL, bucket_start INTEGER NOT NULL, bucket_size INTEGER NOT NULL, samples INTEGER NOT NULL, service_state_avg REAL NOT NULL, service_substate_avg REAL NOT NULL, restart_count_max INTEGER NOT NULL, listen_ports_total_max INTEGER NOT NULL, listen_ports_up_avg REAL NOT NULL, configured_mappings_total_max INTEGER NOT NULL, rx_bytes_max INTEGER NOT NULL, tx_bytes_max INTEGER NOT NULL, PRIMARY KEY(tunnel_id,bucket_start,bucket_size));
INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,1);
"""

def connect_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def _version(conn):
    try: return conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
    except sqlite3.OperationalError: return 0

def init_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = connect_db(db_path); conn.execute("BEGIN IMMEDIATE")
    try:
        version = _version(conn)
        if version == 1:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'metric_samples' in tables and 'metric_samples_v1' not in tables:
                conn.execute("ALTER TABLE metric_samples RENAME TO metric_samples_v1")
        conn.executescript(CREATE_V2)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'metric_samples_v1' in tables:
            conn.execute("""INSERT OR IGNORE INTO metric_samples(sample_id,tunnel_id,collected_at,service_state,service_substate,restart_count,listen_ports_total,listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes)
                       SELECT sample_id,tunnel_id,collected_at,service_state,service_substate,restart_count,listen_ports_total,listen_ports_up,configured_mappings_total,NULLIF(rx_bytes,0),NULLIF(tx_bytes,0) FROM metric_samples_v1""")
            conn.execute("DROP TABLE metric_samples_v1")
        if _version(conn) < 2:
            conn.execute("UPDATE metric_samples SET rx_bytes=NULL WHERE rx_bytes=0")
            conn.execute("UPDATE metric_samples SET tx_bytes=NULL WHERE tx_bytes=0")
            conn.execute("INSERT OR REPLACE INTO schema_migrations(version,applied_at) VALUES(?,?)", (2, int(time.time())))
        conn.commit()
    except Exception: conn.rollback(); raise
    return conn

def parse_env_file(path: str | Path) -> dict[str, str]:
    values = {}
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'): continue
        if '=' not in line: raise ValueError(f"line {lineno}: missing '='")
        key, value = line.split('=', 1); key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key): raise ValueError(f"line {lineno}: invalid key")
        values[key] = shlex.split(value, posix=True)[0] if value.strip() else ""
    return values

def parse_service_name(service_name: str) -> tuple[str, int]:
    m = SERVICE_RE.match(service_name)
    if not m: raise ValueError(f"unsupported service name: {service_name}")
    return m.group(1), int(m.group(2))

def _port(s: str) -> int:
    if not re.match(r"^[1-9][0-9]{0,4}$", s): raise ValueError("invalid port")
    p = int(s)
    if not 1 <= p <= 65535: raise ValueError("invalid port")
    return p

def parse_mappings(value: str) -> tuple[tuple[int, int], ...]:
    if not value or value.startswith(',') or value.endswith(',') or ',,' in value: raise ValueError("MAPPINGS must use listen:target")
    out=[]; seen=set()
    for item in value.split(','):
        if not re.match(r"^[0-9]+:[0-9]+$", item.strip()): raise ValueError(f"invalid mapping: {item}")
        a,b=item.strip().split(':',1); lp,tp=_port(a),_port(b)
        if lp in seen: raise ValueError(f"duplicate listen port: {lp}")
        seen.add(lp); out.append((lp,tp))
    return tuple(out)

def tunnel_from_env(path: str | Path) -> Tunnel:
    p=Path(path); m=ENV_RE.match(p.name)
    if not m: raise ValueError(f"unsupported env name: {p.name}")
    side, number=m.group(1), int(m.group(2)); vals=parse_env_file(p)
    if side == 'iran':
        mappings=parse_mappings(vals.get('MAPPINGS',''))
        return Tunnel(side, number, f"gost-{side}-{number}.service", str(p), tuple(a for a,_ in mappings), tuple(b for _,b in mappings))
    port=_port(vals.get('TUNNEL_PORT',''))
    return Tunnel(side, number, f"gost-{side}-{number}.service", str(p), (port,), ())

def discover_tunnels(env_dir: str | Path = DEFAULT_ENV_DIR) -> tuple[list[Tunnel], list[Event]]:
    root=Path(env_dir); tunnels=[]; events=[]; now=int(time.time())
    if not root.exists(): return [], []
    for p in sorted(root.glob('*.env')):
        if not ENV_RE.match(p.name): continue
        try: tunnels.append(tunnel_from_env(p))
        except Exception as e: events.append(Event(now,'warning','env_parse_error',f"Skipping malformed env file {p.name}", {'path':str(p),'error':str(e)}))
    return tunnels, events

def parse_systemd_properties(text: str) -> dict[str,str]: return dict(line.split('=',1) for line in text.splitlines() if '=' in line)
def _run(cmd: Sequence[str]) -> str: return subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout

def parse_listener_address(local: str) -> tuple[str,int] | None:
    if local.startswith('['):
        host, _, port = local.rpartition(']:'); return (host[1:], _port(port)) if _ else None
    host, sep, port = local.rpartition(':')
    if not sep: return None
    return (host, _port(port))

def parse_ss_listeners(text: str) -> list[dict[str, object]]:
    rows=[]
    for line in text.splitlines():
        m=LISTEN_RE.match(line.strip())
        if not m: continue
        try: addr=parse_listener_address(m.group('local'))
        except ValueError: continue
        if addr: rows.append({'address':addr[0], 'port':addr[1], 'pid': int(m.group('pid') or 0), 'process': m.group('process') or ''})
    return rows

def collect_sample(tunnel: Tunnel, now: int | None = None, runner: Callable[[Sequence[str]], str] = _run) -> MetricSample:
    ts=int(time.time() if now is None else now)
    props=parse_systemd_properties(runner(['systemctl','--no-pager','show',tunnel.service_name,'--property=ActiveState,SubState,NRestarts,MainPID,ExecMainStartTimestampMonotonic']))
    listeners=parse_ss_listeners(runner(['ss','-H','-lntp']))
    owned={r['port'] for r in listeners if r['port'] in tunnel.listen_ports and (not r['process'] or r['process'] in ('gost','nginx'))}
    return MetricSample(tunnel.tunnel_id, ts, int(props.get('ActiveState')=='active'), int(props.get('SubState')=='running'), int(props.get('NRestarts') or 0), len(tunnel.listen_ports), len(owned), len(tunnel.target_ports))

def counter_delta(prev: int|None, cur: int|None, elapsed: float, max_gap: float|None=None) -> CounterDelta:
    if prev is None or cur is None or elapsed <= 0: return CounterDelta(None,None,'unavailable',False,False)
    gap = bool(max_gap is not None and elapsed > max_gap)
    if cur < prev: return CounterDelta(None,None,'unavailable',True,gap)
    d=cur-prev; return CounterDelta(d,d/elapsed,'derived',False,gap)

def read_key_values(path: Path) -> dict[str,int]:
    d={}
    for line in path.read_text().splitlines():
        parts=line.split();
        if len(parts)>=2 and parts[1].isdigit(): d[parts[0].rstrip(':')]=int(parts[1])
    return d

def collect_host_metrics(proc: Path=Path('/proc'), fs_paths: Iterable[Path]=(Path('/'),Path('/etc/gost-manager'),Path('/var/lib/gost-manager'))) -> tuple[list[Metric], list[Event]]:
    metrics=[]; events=[]; ts=int(time.time())
    def unavailable(name, unit='count'): metrics.append(Metric('host',name,None,unit,'unavailable'))
    try:
        cpu=proc.joinpath('stat').read_text().splitlines()[0].split()[1:]; vals=list(map(int,cpu)); metrics.append(Metric('host','cpu_jiffies_total',sum(vals),'jiffies','exact'))
    except Exception as e: unavailable('cpu_jiffies_total','jiffies'); events.append(Event(ts,'warning','proc_stat_unavailable',str(e)))
    try:
        la=proc.joinpath('loadavg').read_text().split(); metrics += [Metric('host','load1',float(la[0]),'load','exact'),Metric('host','load5',float(la[1]),'load','exact'),Metric('host','load15',float(la[2]),'load','exact')]
    except Exception: unavailable('load1','load')
    try:
        mem=read_key_values(proc/'meminfo'); total=mem.get('MemTotal'); avail=mem.get('MemAvailable')
        for k in ('MemTotal','MemAvailable','Buffers','Cached','SwapTotal','SwapFree','Dirty','Writeback'):
            metrics.append(Metric('host',k.lower(),mem.get(k),'KiB','exact' if k in mem else 'unavailable'))
        metrics.append(Metric('host','mem_used', None if total is None or avail is None else total-avail, 'KiB', 'derived' if total is not None and avail is not None else 'unavailable'))
    except Exception: unavailable('memtotal','KiB')
    try:
        for line in (proc/'net/dev').read_text().splitlines()[2:]:
            iface, rest=line.split(':',1); vals=rest.split(); iface=iface.strip(); labels={'interface':iface}; scope='loopback' if iface=='lo' else 'external'
            metrics += [Metric(f'net.{scope}','rx_bytes',int(vals[0]),'bytes','exact',labels),Metric(f'net.{scope}','tx_bytes',int(vals[8]),'bytes','exact',labels),Metric(f'net.{scope}','rx_packets',int(vals[1]),'packets','exact',labels),Metric(f'net.{scope}','tx_packets',int(vals[9]),'packets','exact',labels)]
    except Exception: unavailable('net_dev')
    for name in ('snmp','netstat'):
        try: metrics.append(Metric('host',f'proc_net_{name}_present',1,'bool','exact' if (proc/'net'/name).exists() else 'unavailable'))
        except Exception: unavailable(f'proc_net_{name}_present','bool')
    for n in ('nf_conntrack_count','nf_conntrack_max'):
        p=proc/'sys/net/netfilter'/n
        metrics.append(Metric('conntrack',n, int(p.read_text()) if p.exists() else None, 'count', 'exact' if p.exists() else 'unavailable'))
    try:
        f=(proc/'sys/fs/file-nr').read_text().split(); metrics.append(Metric('host','file_handles_allocated',int(f[0]),'count','exact')); metrics.append(Metric('host','file_handles_max',int((proc/'sys/fs/file-max').read_text()),'count','exact'))
    except Exception: unavailable('file_handles_allocated')
    for fp in fs_paths:
        try: st=os.statvfs(fp); metrics.append(Metric('fs','free_bytes',st.f_bavail*st.f_frsize,'bytes','exact',{'path':str(fp)})); metrics.append(Metric('fs','free_inodes',st.f_favail,'count','exact',{'path':str(fp)}))
        except Exception: metrics.append(Metric('fs','free_bytes',None,'bytes','unavailable',{'path':str(fp)}))
    if not (proc/'diskstats').exists(): metrics.append(Metric('disk','diskstats_present',None,'bool','unavailable'))
    else: metrics.append(Metric('disk','diskstats_present',1,'bool','exact'))
    return metrics, events

def insert_event(conn, event: Event): conn.execute("INSERT INTO events(ts,severity,code,message,details_json) VALUES(?,?,?,?,?)", (event.ts,event.severity,event.code,event.message,json.dumps(event.details,sort_keys=True)))
def upsert_tunnel(conn, t: Tunnel, now: int): conn.execute("INSERT INTO tunnels VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(tunnel_id) DO UPDATE SET env_path=excluded.env_path,listen_ports_json=excluded.listen_ports_json,target_ports_json=excluded.target_ports_json,updated_at=excluded.updated_at", (t.tunnel_id,t.side,t.number,t.service_name,t.env_path,json.dumps(t.listen_ports),json.dumps(t.target_ports),now))
def insert_sample(conn, s: MetricSample) -> int:
    cur=conn.execute("INSERT OR REPLACE INTO metric_samples(tunnel_id,collected_at,service_state,service_substate,restart_count,listen_ports_total,listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes) VALUES(?,?,?,?,?,?,?,?,?,?)", dataclasses.astuple(s)); return cur.lastrowid or conn.execute("SELECT sample_id FROM metric_samples WHERE tunnel_id=? AND collected_at=?",(s.tunnel_id,s.collected_at)).fetchone()[0]
def insert_metric(conn, sample_id: int, m: Metric): conn.execute("INSERT INTO metrics(sample_id,scope,name,value,unit,quality,labels_json) VALUES(?,?,?,?,?,?,?)", (sample_id,m.scope,m.name,m.value,m.unit,m.quality,json.dumps(m.labels,sort_keys=True)))

def rollup_completed_minutes(conn, now: int, interval: float=DEFAULT_SAMPLE_INTERVAL_SECONDS):
    complete=(now//60)*60-60
    if complete < 0: return
    conn.execute("""INSERT OR REPLACE INTO minute_rollups(scope,name,minute_start,samples,min_value,avg_value,max_value,unavailable_count,coverage,unit,quality)
    SELECT scope,name,(collected_at/60)*60,COUNT(*),MIN(value),AVG(value),MAX(value),SUM(CASE WHEN quality='unavailable' THEN 1 ELSE 0 END),MIN(1.0,COUNT(*)/(?)),MAX(unit),CASE WHEN SUM(CASE WHEN quality='unavailable' THEN 1 ELSE 0 END)>0 THEN 'unavailable' ELSE MAX(quality) END
    FROM metrics JOIN metric_samples USING(sample_id) WHERE collected_at < ? GROUP BY scope,name,(collected_at/60)""", (60.0/interval, complete+60))

def apply_retention(conn, now: int):
    rollup_completed_minutes(conn, now)
    conn.execute("DELETE FROM metric_samples WHERE collected_at < ?", (now-RAW_RETENTION_SECONDS,))
    conn.execute("DELETE FROM minute_rollups WHERE minute_start < ?", (now-ROLLUP_RETENTION_SECONDS,))

def collect_once(db_path: str, env_dir: str, now: int | None=None, runner: Callable[[Sequence[str]], str]=_run) -> int:
    ts=int(time.time() if now is None else now); start=time.monotonic(); conn=init_db(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        tunnels, events=discover_tunnels(env_dir)
        for e in events: insert_event(conn,e)
        for t in tunnels:
            upsert_tunnel(conn,t,ts); sid=insert_sample(conn, collect_sample(t,ts,runner)); insert_metric(conn,sid,Metric(f'tunnel.{t.tunnel_id}','listen_ports_up',len(t.listen_ports),'count','exact'))
        hm, he=collect_host_metrics(); sid=insert_sample(conn, MetricSample(None,ts,1,1,0,0,0,0));
        for m in hm+[Metric('collector','duration_seconds',time.monotonic()-start,'seconds','derived'),Metric('collector','tunnels_discovered',len(tunnels),'count','exact')]: insert_metric(conn,sid,m)
        for e in he: insert_event(conn,e)
        apply_retention(conn,ts); conn.commit(); conn.execute('PRAGMA wal_checkpoint(PASSIVE)')
    except Exception as e:
        conn.rollback(); conn.execute('BEGIN IMMEDIATE'); insert_event(conn, Event(ts,'error','collection_error',str(e))); conn.commit()
    finally: conn.close()
    return ts

def scheduler_ticks(start: float, interval: float, durations: Sequence[float]) -> list[float]:
    ticks=[]; nxt=start
    for d in durations:
        ticks.append(nxt); nxt += interval
        end=ticks[-1]+d
        while nxt < end: nxt += interval
    return ticks

def main(argv: Sequence[str] | None=None) -> int:
    p=argparse.ArgumentParser(); p.add_argument('--db',default=os.environ.get('GOST_MONITOR_DB',DEFAULT_DB_PATH)); p.add_argument('--env-dir',default=os.environ.get('GOST_ENV_DIR',DEFAULT_ENV_DIR)); p.add_argument('--now',type=int); p.add_argument('--once',action='store_true')
    a=p.parse_args(argv); collect_once(a.db,a.env_dir,a.now); return 0
if __name__ == '__main__': raise SystemExit(main())
