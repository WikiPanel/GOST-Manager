#!/usr/bin/env python3
import os, sqlite3, tempfile, threading, time, unittest
from pathlib import Path

from monitoring.gost_monitoring import (
    CREATE_SCHEMA, EVENT_RETENTION_SECONDS, RAW_RETENTION_SECONDS, ROLLUP_RETENTION_SECONDS,
    Event, Metric, MetricSample, Tunnel, apply_retention, collect_once, collect_host_metrics,
    collect_sample, counter_delta, discover_tunnels, init_db, insert_event, insert_metric, insert_sample, listener_quality,
    parse_ss_listeners, parse_systemd_properties, quality_worst, open_runtime_database, _cycle, record_cycle_overrun,
    rollup_completed_minutes, run_maintenance, scheduler_ticks, tunnel_from_env, upsert_tunnel,
)

class MonitoringTests(unittest.TestCase):
    def test_production_mappings_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            env=Path(td)/'iran-1.env'; env.write_text('MAPPINGS=80:8080,2052:2052\nKHAREJ_IP=198.51.100.20\nGOST_PASS=12345\n',encoding='utf-8')
            before=env.read_text(); t=tunnel_from_env(env)
            self.assertEqual(t.listen_ports,(80,2052)); self.assertEqual(t.target_ports,(8080,2052)); self.assertEqual(env.read_text(), before)

    def test_production_tunnel_port_not_socks_port(self):
        with tempfile.TemporaryDirectory() as td:
            env=Path(td)/'kharej-2.env'; env.write_text('TUNNEL_PORT=28420\nSOCKS_PORT=9999\nIRAN_IP=203.0.113.77\n',encoding='utf-8')
            self.assertEqual(tunnel_from_env(env).listen_ports,(28420,))

    def test_credentials_ips_not_detected_as_ports_and_legacy_keys_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            env=Path(td)/'iran-1.env'; env.write_text('PORT_MAPPINGS=443:443\nTOKEN=65535\nIP=1.2.3.4\n',encoding='utf-8')
            with self.assertRaises(ValueError): tunnel_from_env(env)
            env.write_text('MAPPINGS=443:443\nTOKEN=65535\nIP=1.2.3.4\n',encoding='utf-8')
            self.assertEqual(tunnel_from_env(env).listen_ports,(443,))

    def test_malformed_env_isolation_structured_event(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td,'iran-1.env').write_text('MAPPINGS=80:80\n',encoding='utf-8')
            Path(td,'kharej-1.env').write_text('TUNNEL_PORT=99999\n',encoding='utf-8')
            tunnels, events=discover_tunnels(td)
            self.assertEqual([t.tunnel_id for t in tunnels], ['iran-1'])
            self.assertEqual(events[0].code, 'env_parse_error')

    def test_ipv4_ipv6_listener_and_remote_rejection(self):
        text='''LISTEN 0 128 0.0.0.0:80 0.0.0.0:* users:(("gost",pid=9,fd=4))\nESTAB 0 0 10.0.0.1:111 8.8.8.8:443\nLISTEN 0 128 [::1]:2052 [::]:* users:(("gost",pid=10,fd=5))\n'''
        rows=parse_ss_listeners(text)
        self.assertEqual([r['port'] for r in rows], [80,2052])
        t=Tunnel('iran',1,'gost-iran-1.service','x',(80,443,2052),(80,443,2052))
        def run(cmd): return 'ActiveState=active\nSubState=running\nNRestarts=1\nMainPID=9\n' if cmd[0]=='systemctl' else text
        s=collect_sample(t,1,run); self.assertEqual(s.listen_ports_up,1)
        def nginx_run(cmd): return 'ActiveState=active\nSubState=running\nNRestarts=1\nMainPID=11\n' if cmd[0]=='systemctl' else 'LISTEN 0 128 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=11,fd=4))\n'
        self.assertEqual(collect_sample(t,1,nginx_run).listen_ports_up,0)
        def missing_run(cmd): return 'ActiveState=active\nSubState=running\nNRestarts=1\nMainPID=9\n' if cmd[0]=='systemctl' else 'LISTEN 0 128 0.0.0.0:80 0.0.0.0:*\n'
        self.assertEqual(listener_quality(t, missing_run), 'unavailable')

    def test_db_wal_busy_timeout_and_v1_migration(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3'); c=sqlite3.connect(db); c.executescript(CREATE_SCHEMA); c.close()
            conn=init_db(db)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
            self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 4)
            self.assertIn(('metric_points',), conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metric_points'").fetchall())

    def test_concurrent_collect_query(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td,'iran-1.env').write_text('MAPPINGS=80:80\n',encoding='utf-8'); db=str(Path(td)/'m.sqlite3')
            def run(cmd): return 'ActiveState=active\nSubState=running\nNRestarts=0\n' if cmd[0]=='systemctl' else 'LISTEN 0 1 0.0.0.0:80 0.0.0.0:* users:(("gost",pid=1,fd=1))\n'
            th=threading.Thread(target=lambda: [collect_once(db,td,100+i,run) for i in range(3)]); th.start()
            conn=init_db(db); [conn.execute('SELECT COUNT(*) FROM metric_samples').fetchone() for _ in range(3)]; th.join()

    def test_counter_delta_reset_gap_cpu_network_interface_loopback(self):
        self.assertEqual(counter_delta(100,160,5).rate, 12)
        self.assertTrue(counter_delta(200,100,5).reset)
        self.assertTrue(counter_delta(100,160,20,12.5).gap)
        with tempfile.TemporaryDirectory() as td:
            proc=Path(td); (proc/'net').mkdir(parents=True); (proc/'sys/net/netfilter').mkdir(parents=True); (proc/'sys/fs').mkdir(parents=True)
            (proc/'stat').write_text('cpu  1 2 3 4 5 6 7 8 0 0\n'); (proc/'loadavg').write_text('0.1 0.2 0.3 1/2 3\n')
            (proc/'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 400 kB\n')
            (proc/'net/dev').write_text('Inter-| Receive | Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\nlo: 10 1 0 0 0 0 0 0 20 2 0 0 0 0 0 0\neth0: 100 10 0 0 0 0 0 0 200 20 0 0 0 0 0 0\n')
            (proc/'sys/fs/file-nr').write_text('1 0 10\n'); (proc/'sys/fs/file-max').write_text('10\n')
            m,_=collect_host_metrics(proc,[Path('/')]); scopes=[x.scope for x in m]
            self.assertIn('net.loopback', scopes); self.assertIn('net.external', scopes); self.assertIn('unavailable', [x.quality for x in m])

    def test_rollups_retention_boundaries_and_rerun(self):
        with tempfile.TemporaryDirectory() as td:
            conn=init_db(str(Path(td)/'m.sqlite3')); upsert_tunnel(conn,Tunnel('iran',1,'gost-iran-1.service','x',(80,),(80,)),0)
            sid=insert_sample(conn,MetricSample('iran-1',60,1,1,0,1,1,1)); insert_metric(conn,sid,Metric('t','x',2,'count','exact'))
            sid=insert_sample(conn,MetricSample('iran-1',119,1,1,0,1,1,1)); insert_metric(conn,sid,Metric('t','x',4,'count','exact'))
            rollup_completed_minutes(conn,180); rollup_completed_minutes(conn,180)
            self.assertEqual(conn.execute('SELECT samples,avg_value,expected_samples FROM minute_rollups WHERE minute_start=60').fetchone(), (2,3.0,6))
            old=10_000_000-RAW_RETENTION_SECONDS-1; insert_sample(conn,MetricSample('iran-1',old,1,1,0,1,1,1)); conn.execute("INSERT OR REPLACE INTO minute_rollups(entity_pk,metric_name,minute_start,samples,expected_samples,unavailable_count,coverage,unit,quality) VALUES(1,'b',?,?,?,?,?,?,?)", (10_000_000-ROLLUP_RETENTION_SECONDS-60,1,12,0,1,'x','exact'))
            apply_retention(conn,10_000_000)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM metric_samples WHERE collected_at=?',(old,)).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM minute_rollups WHERE minute_start<?',(10_000_000-ROLLUP_RETENTION_SECONDS,)).fetchone()[0],0)

    def test_independent_retention_policies_are_bounded_and_idempotent(self):
        now = 10_000_000
        raw_cutoff = now - RAW_RETENTION_SECONDS
        rollup_cutoff = now - ROLLUP_RETENTION_SECONDS
        event_cutoff = now - EVENT_RETENTION_SECONDS
        self.assertEqual(RAW_RETENTION_SECONDS, 6 * 3600)
        self.assertEqual(ROLLUP_RETENTION_SECONDS, 24 * 3600)
        self.assertEqual(EVENT_RETENTION_SECONDS, 24 * 3600)

        with tempfile.TemporaryDirectory() as td:
            conn = init_db(str(Path(td) / 'm.sqlite3'))
            upsert_tunnel(
                conn,
                Tunnel('iran', 1, 'gost-iran-1.service', 'x', (80,), (80,)),
                0,
            )
            for timestamp, name in (
                (raw_cutoff - 1, 'expired_raw'),
                (raw_cutoff, 'retained_raw'),
            ):
                sample_id = insert_sample(
                    conn,
                    MetricSample('iran-1', timestamp, 1, 1, 0, 1, 1, 1),
                )
                insert_metric(
                    conn,
                    sample_id,
                    Metric('retention', name, 1, 'count', 'exact'),
                )
            entity_pk = conn.execute(
                "SELECT entity_pk FROM entities WHERE entity_type='tunnel' "
                "AND entity_id='iran-1'"
            ).fetchone()[0]
            for minute_start, name in (
                (rollup_cutoff - 60, 'expired_rollup'),
                (rollup_cutoff, 'retained_rollup'),
            ):
                conn.execute(
                    "INSERT INTO minute_rollups(entity_pk,metric_name,minute_start,"
                    "samples,expected_samples,unavailable_count,coverage,unit,quality) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (entity_pk, name, minute_start, 1, 1, 0, 1.0, 'count', 'exact'),
                )
            insert_event(
                conn,
                Event(event_cutoff - 1, 'info', 'expired_event', 'expired'),
            )
            insert_event(
                conn,
                Event(event_cutoff, 'info', 'retained_event', 'retained'),
            )
            conn.execute(
                "INSERT OR REPLACE INTO collector_state(key,value) VALUES(?,?)",
                ('minute_rollup_watermark', str((now // 60) * 60)),
            )

            def retained_state():
                return (
                    conn.execute(
                        "SELECT collected_at FROM metric_samples ORDER BY collected_at"
                    ).fetchall(),
                    conn.execute(
                        "SELECT ts FROM metric_points ORDER BY ts"
                    ).fetchall(),
                    conn.execute(
                        "SELECT minute_start FROM minute_rollups ORDER BY minute_start"
                    ).fetchall(),
                    conn.execute(
                        "SELECT ts,code FROM events ORDER BY ts"
                    ).fetchall(),
                )

            conn.execute('BEGIN IMMEDIATE')
            run_maintenance(conn, now)
            conn.commit()
            first = retained_state()
            conn.execute('BEGIN IMMEDIATE')
            run_maintenance(conn, now)
            conn.commit()
            second = retained_state()

            self.assertEqual(first, second)
            self.assertEqual(first[0], [(raw_cutoff,)])
            self.assertEqual(first[1], [(raw_cutoff,)])
            self.assertEqual(first[2], [(rollup_cutoff,)])
            self.assertEqual(first[3], [(event_cutoff, 'retained_event')])

    def test_structured_events_self_metrics_optional_sources_scheduler_pid_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td,'bad-1.env').write_text('X=1\n'); Path(td,'iran-1.env').write_text('MAPPINGS=80:80\n')
            db=str(Path(td)/'m.sqlite3')
            collect_once(db,td,100,lambda cmd: 'ActiveState=active\nSubState=running\nNRestarts=0\nMainPID=1\n' if cmd[0]=='systemctl' else '')
            conn=init_db(db)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM metrics WHERE scope='collector'").fetchone()[0],0)
            self.assertEqual(scheduler_ticks(0,5,[1,8,1]), [0,5,15])
            props1=parse_systemd_properties('MainPID=1\nExecMainStartTimestampMonotonic=10\n')
            props2=parse_systemd_properties('MainPID=2\nExecMainStartTimestampMonotonic=20\n')
            self.assertNotEqual((props1['MainPID'],props1['ExecMainStartTimestampMonotonic']),(props2['MainPID'],props2['ExecMainStartTimestampMonotonic']))

    def test_quality_precedence_and_injected_migration_rollback(self):
        self.assertEqual(quality_worst(['exact', 'derived', 'estimated']), 'estimated')
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3')
            with self.assertRaises(RuntimeError):
                init_db(db, inject_failure='after_create')
            conn=init_db(db)
            self.assertEqual(conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 4)

    def test_once_cli_fails_when_collection_fails(self):
        import monitoring.gost_monitoring as gm
        with tempfile.TemporaryDirectory() as td:
            Path(td,'iran-1.env').write_text('MAPPINGS=80:80\n',encoding='utf-8')
            old = gm.collect_once
            try:
                gm.collect_once = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))
                self.assertEqual(gm.main(['--policy', 'generic', '--db', str(Path(td)/'m.sqlite3'), '--env-dir', td, '--once']), 1)
            finally:
                gm.collect_once = old

class MonitoringIssue13Tests(unittest.TestCase):
    def test_once_with_maintenance_persists_checkpoint_and_rollup(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td,'iran-1.env').write_text('MAPPINGS=80:80\n',encoding='utf-8')
            db=str(Path(td)/'m.sqlite3')
            seed=init_db(db)
            sid=insert_sample(seed,MetricSample(None,60,1,1,0,0,0,0))
            insert_metric(seed,sid,Metric('host','seed',1,'count','exact',entity_type='host',entity_id='local'))
            seed.close()
            def run(cmd):
                if cmd[0]=='systemctl':
                    return 'ActiveState=active\nSubState=running\nNRestarts=0\nMainPID=1\n'
                return 'LISTEN 0 1 0.0.0.0:80 0.0.0.0:* users:(("gost",pid=1,fd=1))\n'
            self.assertEqual(collect_once(db, td, 180, run, maintenance=True), 180)
            conn=init_db(db)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertGreaterEqual(conn.execute('SELECT COUNT(*) FROM sample_cycles').fetchone()[0], 2)
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM metric_points').fetchone()[0], 0)
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM minute_rollups').fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE code='wal_checkpoint'").fetchone()[0], 1)

    def test_populated_v1_migration_multiple_tunnels_same_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3')
            c=sqlite3.connect(db)
            c.executescript(CREATE_SCHEMA)
            c.execute("INSERT INTO tunnels(tunnel_id,side,tunnel_number,service_name,env_path,listen_ports_json,target_ports_json,updated_at) VALUES('iran-1','iran',1,'gost-iran-1.service','a','[80]','[80]',1)")
            c.execute("INSERT INTO tunnels(tunnel_id,side,tunnel_number,service_name,env_path,listen_ports_json,target_ports_json,updated_at) VALUES('iran-2','iran',2,'gost-iran-2.service','b','[81]','[81]',1)")
            c.execute("INSERT INTO metric_samples(tunnel_id,collected_at,service_state,service_substate,restart_count,listen_ports_total,listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes) VALUES('iran-1',100,1,1,0,1,1,1,10,20)")
            c.execute("INSERT INTO metric_samples(tunnel_id,collected_at,service_state,service_substate,restart_count,listen_ports_total,listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes) VALUES('iran-2',100,1,1,0,1,1,1,30,40)")
            c.commit(); c.close()
            conn=init_db(db)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tunnels').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM metric_samples').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(DISTINCT cycle_id) FROM metric_samples').fetchone()[0], 1)

    def test_v2_incompatible_tables_are_renamed_and_recreated(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3')
            c=sqlite3.connect(db)
            c.executescript('''
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
            INSERT INTO schema_migrations VALUES(2,2);
            CREATE TABLE tunnels(tunnel_id TEXT PRIMARY KEY, side TEXT NOT NULL CHECK(side IN('iran','kharej')), tunnel_number INTEGER NOT NULL, service_name TEXT NOT NULL UNIQUE, env_path TEXT NOT NULL, listen_ports_json TEXT NOT NULL DEFAULT '[]', target_ports_json TEXT NOT NULL DEFAULT '[]', updated_at INTEGER NOT NULL, UNIQUE(side,tunnel_number));
            CREATE TABLE metric_samples(sample_id INTEGER PRIMARY KEY AUTOINCREMENT, tunnel_id TEXT REFERENCES tunnels(tunnel_id) ON DELETE CASCADE, collected_at INTEGER NOT NULL, service_state INTEGER NOT NULL DEFAULT 0, service_substate INTEGER NOT NULL DEFAULT 0, restart_count INTEGER NOT NULL DEFAULT 0, listen_ports_total INTEGER NOT NULL DEFAULT 0, listen_ports_up INTEGER NOT NULL DEFAULT 0, configured_mappings_total INTEGER NOT NULL DEFAULT 0, rx_bytes INTEGER, tx_bytes INTEGER, UNIQUE(tunnel_id,collected_at));
            CREATE INDEX idx_metric_samples_time ON metric_samples(collected_at);
            CREATE TABLE metrics(sample_id INTEGER NOT NULL REFERENCES metric_samples(sample_id) ON DELETE CASCADE, scope TEXT NOT NULL, name TEXT NOT NULL, value REAL, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), labels_json TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX idx_metrics_lookup ON metrics(scope,name);
            CREATE TABLE events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, severity TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX idx_events_time ON events(ts);
            CREATE TABLE minute_rollups(scope TEXT NOT NULL, name TEXT NOT NULL, minute_start INTEGER NOT NULL, samples INTEGER NOT NULL, min_value REAL, avg_value REAL, max_value REAL, unavailable_count INTEGER NOT NULL, reset_count INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0, coverage REAL NOT NULL, unit TEXT NOT NULL, quality TEXT NOT NULL, PRIMARY KEY(scope,name,minute_start));
            CREATE INDEX idx_minute_rollups_time ON minute_rollups(minute_start);
            CREATE TABLE collector_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO tunnels VALUES('iran-1','iran',1,'gost-iran-1.service','a','[80]','[80]',2);
            INSERT INTO metric_samples(sample_id,tunnel_id,collected_at,service_state,service_substate,restart_count,listen_ports_total,listen_ports_up,configured_mappings_total,rx_bytes,tx_bytes) VALUES(1,'iran-1',100,1,1,0,1,1,1,10,20);
            INSERT INTO metrics VALUES(1,'collector','duration_seconds',0.1,'seconds','derived','{}');
            INSERT INTO minute_rollups VALUES('collector','duration_seconds',60,1,0.1,0.1,0.1,0,0,0,1.0,'seconds','derived');
            ''')
            c.commit(); c.close()
            conn=init_db(db)
            self.assertEqual(conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 4)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tunnels').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM metric_samples').fetchone()[0],1)
            self.assertGreaterEqual(conn.execute('SELECT COUNT(*) FROM metric_points').fetchone()[0],1)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertFalse([r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%legacy%'")])

    def test_old_v3_migration_adds_v4_columns_and_unique_points(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3')
            c=sqlite3.connect(db)
            c.executescript('''
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
            INSERT INTO schema_migrations VALUES(3,3);
            CREATE TABLE sample_cycles(cycle_id INTEGER PRIMARY KEY AUTOINCREMENT, collected_at INTEGER NOT NULL UNIQUE, monotonic_started REAL NOT NULL, monotonic_finished REAL NOT NULL, duration_seconds REAL NOT NULL, success INTEGER NOT NULL, overrun INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE entities(entity_pk INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, display_name TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL, UNIQUE(entity_type,entity_id));
            CREATE TABLE tunnels(tunnel_id TEXT PRIMARY KEY, entity_pk INTEGER REFERENCES entities(entity_pk) ON DELETE SET NULL, side TEXT NOT NULL CHECK(side IN('iran','kharej')), tunnel_number INTEGER NOT NULL, service_name TEXT NOT NULL UNIQUE, env_path TEXT NOT NULL, listen_ports_json TEXT NOT NULL DEFAULT '[]', target_ports_json TEXT NOT NULL DEFAULT '[]', updated_at INTEGER NOT NULL, UNIQUE(side,tunnel_number));
            CREATE TABLE metric_samples(sample_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL REFERENCES sample_cycles(cycle_id) ON DELETE CASCADE, tunnel_id TEXT REFERENCES tunnels(tunnel_id) ON DELETE CASCADE, collected_at INTEGER NOT NULL, service_state INTEGER NOT NULL DEFAULT 0, service_substate INTEGER NOT NULL DEFAULT 0, restart_count INTEGER NOT NULL DEFAULT 0, listen_ports_total INTEGER NOT NULL DEFAULT 0, listen_ports_up INTEGER NOT NULL DEFAULT 0, configured_mappings_total INTEGER NOT NULL DEFAULT 0, rx_bytes INTEGER, tx_bytes INTEGER, UNIQUE(cycle_id,tunnel_id));
            CREATE TABLE metric_points(point_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL REFERENCES sample_cycles(cycle_id) ON DELETE CASCADE, entity_pk INTEGER NOT NULL REFERENCES entities(entity_pk) ON DELETE CASCADE, metric_name TEXT NOT NULL, ts INTEGER NOT NULL, numeric_value REAL, text_value TEXT, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), reset INTEGER NOT NULL DEFAULT 0, gap INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE metrics(sample_id INTEGER NOT NULL REFERENCES metric_samples(sample_id) ON DELETE CASCADE, scope TEXT NOT NULL, name TEXT NOT NULL, value REAL, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), labels_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE minute_rollups(entity_pk INTEGER NOT NULL REFERENCES entities(entity_pk) ON DELETE CASCADE, metric_name TEXT NOT NULL, minute_start INTEGER NOT NULL, samples INTEGER NOT NULL, expected_samples INTEGER NOT NULL, min_value REAL, avg_value REAL, max_value REAL, unavailable_count INTEGER NOT NULL, reset_count INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0, coverage REAL NOT NULL, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), PRIMARY KEY(entity_pk,metric_name,minute_start));
            CREATE TABLE events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, severity TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE collector_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO sample_cycles VALUES(1,100,1.0,1.1,0.1,1,0);
            INSERT INTO entities VALUES(1,'collector','local','local','{}',100);
            INSERT INTO metric_samples(sample_id,cycle_id,tunnel_id,collected_at) VALUES(1,1,NULL,100);
            INSERT INTO metric_points(cycle_id,entity_pk,metric_name,ts,numeric_value,unit,quality) VALUES(1,1,'duration_seconds',100,0.1,'seconds','derived');
            INSERT INTO metrics VALUES(1,'collector','duration_seconds',0.1,'seconds','derived','{}');
            ''')
            c.commit(); c.close()
            conn=init_db(db)
            self.assertEqual(conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0],4)
            self.assertIn('missed_deadlines', {r[1] for r in conn.execute('PRAGMA table_info(sample_cycles)')})
            self.assertGreaterEqual(conn.execute('SELECT COUNT(*) FROM metric_points').fetchone()[0],1)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            Path(td,'iran-1.env').write_text('MAPPINGS=80:80\n',encoding='utf-8')
            collect_once(db,td,200,lambda cmd:'')
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM sample_cycles').fetchone()[0],1)


    def test_metric_points_idempotent_and_unavailable_rollup(self):
        with tempfile.TemporaryDirectory() as td:
            conn=init_db(str(Path(td)/'m.sqlite3'))
            sid=insert_sample(conn,MetricSample(None,60,1,1,0,0,0,0))
            insert_metric(conn,sid,Metric('host','x',None,'count','unavailable',entity_type='host',entity_id='local'))
            insert_metric(conn,sid,Metric('host','x',None,'count','unavailable',entity_type='host',entity_id='local'))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM metric_points WHERE metric_name='x'").fetchone()[0],1)
            rollup_completed_minutes(conn,120)
            self.assertEqual(conn.execute("SELECT samples,unavailable_count,min_value,avg_value,max_value,quality FROM minute_rollups WHERE metric_name='x'").fetchone(), (1,1,None,None,None,'unavailable'))

    def test_one_ss_and_one_systemd_per_service_per_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            for i,p in enumerate((80,81,82),1):
                Path(td,f'iran-{i}.env').write_text(f'MAPPINGS={p}:{p}\n',encoding='utf-8')
            db=str(Path(td)/'m.sqlite3'); calls=[]
            def run(cmd):
                calls.append(tuple(cmd))
                if cmd[0]=='systemctl':
                    return 'ActiveState=active\nSubState=running\nNRestarts=0\nMainPID=1\n'
                return '\n'.join(f'LISTEN 0 1 0.0.0.0:{p} 0.0.0.0:* users:(("gost",pid=1,fd=1))' for p in (80,81,82))
            collect_once(db,td,100,run)
            self.assertEqual(sum(1 for c in calls if c[:2]==('ss','-H')),2)
            self.assertEqual(sum(1 for c in calls if c and c[0]=='systemctl'),4)

    def test_concurrent_readers_no_corruption(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td,'iran-1.env').write_text('MAPPINGS=80:80\n',encoding='utf-8'); db=str(Path(td)/'m.sqlite3')
            errors=[]
            def run(cmd): return 'ActiveState=active\nSubState=running\nNRestarts=0\nMainPID=1\n' if cmd[0]=='systemctl' else 'LISTEN 0 1 0.0.0.0:80 0.0.0.0:* users:(("gost",pid=1,fd=1))\n'
            def writer():
                try:
                    for i in range(5): collect_once(db,td,200+i,run)
                except Exception as e: errors.append(e)
            def reader():
                try:
                    for _ in range(20):
                        c=init_db(db); c.execute('SELECT COUNT(*) FROM metric_points').fetchone(); c.close()
                except Exception as e: errors.append(e)
            threads=[threading.Thread(target=writer), threading.Thread(target=reader), threading.Thread(target=reader)]
            [t.start() for t in threads]; [t.join(5) for t in threads]
            self.assertTrue(all(not t.is_alive() for t in threads))
            self.assertEqual(errors, [])
            c=init_db(db)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sample_cycles').fetchone()[0],5)
            self.assertGreater(c.execute('SELECT COUNT(*) FROM metric_points').fetchone()[0],5)

    def test_daemon_timing_and_signal_stop(self):
        import monitoring.gost_monitoring as gm
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "m.sqlite3")
            env_dir = str(Path(td) / "env")
            Path(env_dir).mkdir()

            calls = []
            sleeps = []
            mono = [0.0]
            wall = [1000.0]
            stops = [False]
            clock = gm.Clock(lambda: wall[0], lambda: mono[0])

            def fake_collect(*args, **kwargs):
                calls.append(kwargs.copy())
                mono[0] += 12.0
                wall[0] += 12.0
                return int(wall[0])

            def fake_record(db, ts, finished, deadline, interval):
                calls[-1]["recorded"] = (
                    ts,
                    finished,
                    deadline,
                    interval,
                )
                stops[0] = True

            old_collect = gm.collect_once
            old_record = gm.record_cycle_overrun
            gm.collect_once = fake_collect
            gm.record_cycle_overrun = fake_record

            try:
                self.assertEqual(
                    gm.run_daemon(
                        db_path,
                        env_dir,
                        interval=5,
                        maintenance_interval=60,
                        clock=clock,
                        sleeper=lambda seconds: (
                            sleeps.append(seconds),
                            mono.__setitem__(0, mono[0] + seconds),
                        ),
                        stop_requested=lambda: stops[0],
                    ),
                    0,
                )
            finally:
                gm.collect_once = old_collect
                gm.record_cycle_overrun = old_record

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["recorded"][2], 0.0)
            self.assertEqual(calls[0]["recorded"][3], 5)

    def test_retry_same_cycle_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3')
            def run(cmd): return ''
            collect_once(db,td,300,run)
            conn=init_db(db)
            counts1=(conn.execute('SELECT COUNT(*) FROM metric_samples').fetchone()[0], conn.execute('SELECT COUNT(*) FROM metric_points').fetchone()[0], conn.execute('SELECT COUNT(*) FROM metrics').fetchone()[0])
            conn.close()
            collect_once(db,td,300,run)
            conn=init_db(db)
            counts2=(conn.execute('SELECT COUNT(*) FROM metric_samples').fetchone()[0], conn.execute('SELECT COUNT(*) FROM metric_points').fetchone()[0], conn.execute('SELECT COUNT(*) FROM metrics').fetchone()[0])
            self.assertEqual(counts1, counts2)

    def test_checkpoint_failure_preserves_successful_collection(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3')
            collect_once(db,td,360,lambda cmd:'',maintenance=True,checkpoint=lambda path: (_ for _ in ()).throw(RuntimeError('ckpt boom')))
            conn=init_db(db)
            self.assertEqual(conn.execute('SELECT success FROM sample_cycles WHERE collected_at=360').fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE code='wal_checkpoint_failed'").fetchone()[0],1)

    def test_completed_minute_cutoff_does_not_advance_over_current_minute(self):
        with tempfile.TemporaryDirectory() as td:
            conn=init_db(str(Path(td)/'m.sqlite3'))
            sid=insert_sample(conn,MetricSample(None,60,1,1,0,0,0,0))
            insert_metric(conn,sid,Metric('host','done',1,'count','exact',entity_type='host',entity_id='local'))
            sid=insert_sample(conn,MetricSample(None,125,1,1,0,0,0,0))
            insert_metric(conn,sid,Metric('host','current',2,'count','exact',entity_type='host',entity_id='local'))
            rollup_completed_minutes(conn,125)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM minute_rollups WHERE metric_name='done'").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM minute_rollups WHERE metric_name='current'").fetchone()[0],0)
            sid=insert_sample(conn,MetricSample(None,130,1,1,0,0,0,0))
            insert_metric(conn,sid,Metric('host','current',3,'count','exact',entity_type='host',entity_id='local'))
            rollup_completed_minutes(conn,180)
            self.assertEqual(conn.execute("SELECT samples FROM minute_rollups WHERE metric_name='current'").fetchone()[0],2)


if __name__ == '__main__': unittest.main()

class MonitoringReviewP0Tests(unittest.TestCase):
    def test_metrics_view_is_one_row_per_point(self):
        with tempfile.TemporaryDirectory() as td:
            conn=init_db(str(Path(td)/'m.sqlite3'))
            cid=conn.execute("INSERT INTO sample_cycles(collected_at,monotonic_started,monotonic_finished,duration_seconds,success,overrun) VALUES(500,0,1,1,1,0)").lastrowid
            sid=insert_sample(conn,MetricSample(None,500,1,1,0,0,0,0),cid)
            insert_metric(conn,sid,Metric('host','load1',1,'load','exact',entity_type='host',entity_id='local'),cid,500)
            for i in range(3):
                t=Tunnel('iran',i+1,f'gost-iran-{i+1}.service','x',(80+i,),(80+i,))
                upsert_tunnel(conn,t,500)
                sid=insert_sample(conn,MetricSample(t.tunnel_id,500,1,1,0,1,1,1),cid)
                insert_metric(conn,sid,Metric(f'tunnel.{t.tunnel_id}','listen_ports_up',1,'count','exact',entity_type='tunnel',entity_id=t.tunnel_id),cid,500)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM metrics').fetchone()[0], conn.execute('SELECT COUNT(*) FROM metric_points').fetchone()[0])
            self.assertEqual(conn.execute("SELECT SUM(value) FROM metrics WHERE name='listen_ports_up'").fetchone()[0],3)

    def test_post_commit_maintenance_failures_do_not_fail_collection(self):
        failures = [
            {'checkpoint': lambda path: (_ for _ in ()).throw(RuntimeError('checkpoint'))},
            {'maintenance_conn_factory': lambda path: (_ for _ in ()).throw(RuntimeError('open'))},
            {'maintenance_conn_factory': lambda path: BrokenBegin(init_db(path))},
            {'checkpoint_event_writer': lambda conn,event: (_ for _ in ()).throw(RuntimeError('event'))},
            {'maintenance_conn_factory': lambda path: BrokenCommit(init_db(path))},
        ]
        for idx, kwargs in enumerate(failures):
            with tempfile.TemporaryDirectory() as td:
                db=str(Path(td)/'m.sqlite3')
                collect_once(db,td,600+idx,lambda cmd:'',maintenance=True,**kwargs)
                conn=init_db(db)
                self.assertEqual(conn.execute('SELECT success FROM sample_cycles WHERE collected_at=?',(600+idx,)).fetchone()[0],1)
                self.assertGreater(conn.execute('SELECT COUNT(*) FROM metric_points').fetchone()[0],0)

    def test_v2_migration_preserves_label_entity_identity(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3'); c=sqlite3.connect(db)
            c.executescript('''
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL); INSERT INTO schema_migrations VALUES(2,2);
            CREATE TABLE tunnels(tunnel_id TEXT PRIMARY KEY, side TEXT NOT NULL CHECK(side IN('iran','kharej')), tunnel_number INTEGER NOT NULL, service_name TEXT NOT NULL UNIQUE, env_path TEXT NOT NULL, listen_ports_json TEXT NOT NULL DEFAULT '[]', target_ports_json TEXT NOT NULL DEFAULT '[]', updated_at INTEGER NOT NULL, UNIQUE(side,tunnel_number));
            CREATE TABLE metric_samples(sample_id INTEGER PRIMARY KEY AUTOINCREMENT, tunnel_id TEXT REFERENCES tunnels(tunnel_id) ON DELETE CASCADE, collected_at INTEGER NOT NULL, service_state INTEGER NOT NULL DEFAULT 0, service_substate INTEGER NOT NULL DEFAULT 0, restart_count INTEGER NOT NULL DEFAULT 0, listen_ports_total INTEGER NOT NULL DEFAULT 0, listen_ports_up INTEGER NOT NULL DEFAULT 0, configured_mappings_total INTEGER NOT NULL DEFAULT 0, rx_bytes INTEGER, tx_bytes INTEGER, UNIQUE(tunnel_id,collected_at));
            CREATE TABLE metrics(sample_id INTEGER NOT NULL REFERENCES metric_samples(sample_id) ON DELETE CASCADE, scope TEXT NOT NULL, name TEXT NOT NULL, value REAL, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), labels_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE minute_rollups(scope TEXT NOT NULL, name TEXT NOT NULL, minute_start INTEGER NOT NULL, samples INTEGER NOT NULL, min_value REAL, avg_value REAL, max_value REAL, unavailable_count INTEGER NOT NULL, reset_count INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0, coverage REAL NOT NULL, unit TEXT NOT NULL, quality TEXT NOT NULL, PRIMARY KEY(scope,name,minute_start));
            CREATE TABLE events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, severity TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE collector_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metric_samples(sample_id,tunnel_id,collected_at) VALUES(1,NULL,100);
            INSERT INTO metrics VALUES(1,'net.external','rx_bytes',10,'bytes','exact','{"interface":"eth0"}');
            INSERT INTO metrics VALUES(1,'net.external','rx_bytes',20,'bytes','exact','{"interface":"eth1"}');
            INSERT INTO metrics VALUES(1,'fs','free_bytes',30,'bytes','exact','{"path":"/"}');
            INSERT INTO metrics VALUES(1,'fs','free_bytes',40,'bytes','exact','{"path":"/var/lib/gost-manager"}');
            '''); c.commit(); c.close()
            conn=init_db(db)
            ids={r[0] for r in conn.execute('SELECT entity_id FROM entities')}
            self.assertTrue({'interface:eth0','interface:eth1','fs:/','fs:/var/lib/gost-manager'}.issubset(ids))
            self.assertEqual(conn.execute('SELECT SUM(numeric_value) FROM metric_points').fetchone()[0],100)

    def test_old_v3_metric_points_precede_metrics_and_host_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3'); c=sqlite3.connect(db)
            c.executescript('''
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL); INSERT INTO schema_migrations VALUES(3,3);
            CREATE TABLE sample_cycles(cycle_id INTEGER PRIMARY KEY AUTOINCREMENT, collected_at INTEGER NOT NULL UNIQUE, monotonic_started REAL NOT NULL, monotonic_finished REAL NOT NULL, duration_seconds REAL NOT NULL, success INTEGER NOT NULL, overrun INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE entities(entity_pk INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, display_name TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL, UNIQUE(entity_type,entity_id));
            CREATE TABLE metric_samples(sample_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL REFERENCES sample_cycles(cycle_id) ON DELETE CASCADE, tunnel_id TEXT REFERENCES tunnels(tunnel_id) ON DELETE CASCADE, collected_at INTEGER NOT NULL, service_state INTEGER NOT NULL DEFAULT 0, service_substate INTEGER NOT NULL DEFAULT 0, restart_count INTEGER NOT NULL DEFAULT 0, listen_ports_total INTEGER NOT NULL DEFAULT 0, listen_ports_up INTEGER NOT NULL DEFAULT 0, configured_mappings_total INTEGER NOT NULL DEFAULT 0, rx_bytes INTEGER, tx_bytes INTEGER, UNIQUE(cycle_id,tunnel_id));
            CREATE TABLE tunnels(tunnel_id TEXT PRIMARY KEY, entity_pk INTEGER REFERENCES entities(entity_pk) ON DELETE SET NULL, side TEXT NOT NULL CHECK(side IN('iran','kharej')), tunnel_number INTEGER NOT NULL, service_name TEXT NOT NULL UNIQUE, env_path TEXT NOT NULL, listen_ports_json TEXT NOT NULL DEFAULT '[]', target_ports_json TEXT NOT NULL DEFAULT '[]', updated_at INTEGER NOT NULL, UNIQUE(side,tunnel_number));
            CREATE TABLE metric_points(point_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL REFERENCES sample_cycles(cycle_id) ON DELETE CASCADE, entity_pk INTEGER NOT NULL REFERENCES entities(entity_pk) ON DELETE CASCADE, metric_name TEXT NOT NULL, ts INTEGER NOT NULL, numeric_value REAL, text_value TEXT, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), reset INTEGER NOT NULL DEFAULT 0, gap INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE metrics(sample_id INTEGER NOT NULL REFERENCES metric_samples(sample_id) ON DELETE CASCADE, scope TEXT NOT NULL, name TEXT NOT NULL, value REAL, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), labels_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE minute_rollups(entity_pk INTEGER NOT NULL REFERENCES entities(entity_pk) ON DELETE CASCADE, metric_name TEXT NOT NULL, minute_start INTEGER NOT NULL, samples INTEGER NOT NULL, expected_samples INTEGER NOT NULL, min_value REAL, avg_value REAL, max_value REAL, unavailable_count INTEGER NOT NULL, reset_count INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0, coverage REAL NOT NULL, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), PRIMARY KEY(entity_pk,metric_name,minute_start));
            CREATE TABLE events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, severity TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'); CREATE TABLE collector_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO sample_cycles VALUES(1,100,0,12,12,1,0); INSERT INTO entities VALUES(1,'collector','local','local','{}',100);
            INSERT INTO metric_samples(sample_id,cycle_id,tunnel_id,collected_at) VALUES(1,1,NULL,100); INSERT INTO metric_samples(sample_id,cycle_id,tunnel_id,collected_at) VALUES(2,1,NULL,100);
            INSERT INTO metric_points(cycle_id,entity_pk,metric_name,ts,numeric_value,unit,quality) VALUES(1,1,'duration_seconds',100,12,'seconds','derived');
            INSERT INTO metrics VALUES(2,'collector','duration_seconds',99,'seconds','derived','{}');
            '''); c.commit(); c.close()
            conn=init_db(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM metric_points WHERE metric_name='duration_seconds'").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT numeric_value FROM metric_points WHERE metric_name='duration_seconds'").fetchone()[0],12)
            self.assertFalse(conn.execute("SELECT 1 FROM entities WHERE entity_type='collector' AND entity_id='collector'").fetchone())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM metric_samples WHERE cycle_id=1 AND sample_identity="host"').fetchone()[0],1)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_legacy_minute_rollups_are_archived(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3'); c=sqlite3.connect(db)
            c.executescript('''
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL); INSERT INTO schema_migrations VALUES(2,2);
            CREATE TABLE minute_rollups(scope TEXT NOT NULL, name TEXT NOT NULL, minute_start INTEGER NOT NULL, samples INTEGER NOT NULL, min_value REAL, avg_value REAL, max_value REAL, unavailable_count INTEGER NOT NULL, reset_count INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0, coverage REAL NOT NULL, unit TEXT NOT NULL, quality TEXT NOT NULL, PRIMARY KEY(scope,name,minute_start));
            INSERT INTO minute_rollups VALUES('collector','x',60,1,1,1,1,0,0,0,1,'count','exact');
            '''); c.commit(); c.close()
            conn=init_db(db)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM minute_rollups_archive').fetchone()[0],1)

    def test_failed_overrun_cycle_records_timing(self):
        import monitoring.gost_monitoring as gm
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3'); mono=[0.0]; wall=[1000.0]; stop=[False]
            clock=gm.Clock(lambda: wall[0], lambda: mono[0])
            def bad_collect(*args, **kwargs):
                conn=init_db(db); conn.execute('BEGIN IMMEDIATE'); gm._cycle(conn,1000,0,12,12,False,False); conn.commit(); conn.close()
                mono[0]=12; wall[0]=1012
                raise gm.CollectionCycleError(1000,'boom')
            old=gm.collect_once; gm.collect_once=bad_collect
            try:
                gm.run_daemon(db,td,interval=5,clock=clock,sleeper=lambda s: mono.__setitem__(0,mono[0]+s),stop_requested=lambda: mono[0]>=12)
            finally:
                gm.collect_once=old
            conn=init_db(db)
            row=conn.execute('SELECT success,overrun,missed_deadlines,overrun_seconds FROM sample_cycles WHERE collected_at=1000').fetchone()
            self.assertEqual(row[0],0); self.assertEqual(row[1],1); self.assertEqual(row[2],2); self.assertEqual(row[3],7.0)

class BrokenBegin:
    def __init__(self, conn): self.conn=conn
    def execute(self, sql, *args):
        if sql == 'BEGIN IMMEDIATE': raise RuntimeError('begin')
        return self.conn.execute(sql,*args)
    def rollback(self): return self.conn.rollback()
    def close(self): return self.conn.close()

class BrokenCommit:
    def __init__(self, conn): self.conn=conn
    def execute(self, sql, *args): return self.conn.execute(sql,*args)
    def commit(self): raise RuntimeError('commit')
    def rollback(self): return self.conn.rollback()
    def close(self): return self.conn.close()

class MonitoringRuntimeSeparationTests(unittest.TestCase):
    def test_runtime_open_healthy_v4_avoids_migration_work(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3')
            conn=init_db(db)
            cid=conn.execute("INSERT INTO sample_cycles(collected_at,monotonic_started,monotonic_finished,duration_seconds,success,overrun) VALUES(700,0,1,1,1,0)").lastrowid
            sid=insert_sample(conn,MetricSample(None,700,1,1,0,0,0,0),cid)
            for i in range(3000):
                insert_metric(conn,sid,Metric('host',f'm{i}',i,'count','exact',entity_type='host',entity_id='local'),cid,700)
            conn.commit(); conn.close()
            traced=[]
            old=sqlite3.connect
            def traced_connect(*args, **kwargs):
                c=old(*args, **kwargs)
                c.set_trace_callback(lambda sql: traced.append(sql.upper()))
                return c
            sqlite3.connect=traced_connect
            try:
                c=open_runtime_database(db); c.close()
                c=open_runtime_database(db); c.close()
            finally:
                sqlite3.connect=old
            forbidden=('CREATE TABLE','ALTER TABLE','DROP TABLE','DROP VIEW','FOREIGN_KEY_CHECK','DELETE FROM METRIC_SAMPLES')
            self.assertFalse([sql for sql in traced if any(f in sql for f in forbidden)])
            self.assertFalse([sql for sql in traced if 'COUNT(*)' in sql])

    def test_deadline_boundary_calculation(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3'); init_db(db).close()
            for ts,finish,expected in ((800,5.000,(0,0,0.0)),(801,5.001,(1,1,0.001)),(802,12.000,(1,2,7.0))):
                conn=open_runtime_database(db); conn.execute('BEGIN IMMEDIATE'); _cycle(conn,ts,0,finish,finish,True,False); conn.commit(); conn.close()
                record_cycle_overrun(db,ts,finish,0.0,5.0)
                row=open_runtime_database(db).execute('SELECT overrun,missed_deadlines,overrun_seconds FROM sample_cycles WHERE collected_at=?',(ts,)).fetchone()
                self.assertEqual(row[0],expected[0]); self.assertEqual(row[1],expected[1]); self.assertAlmostEqual(row[2],expected[2],places=3)

    def test_old_v3_tunnel_scope_precedence_with_empty_labels(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3'); c=sqlite3.connect(db)
            c.executescript('''
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL); INSERT INTO schema_migrations VALUES(3,3);
            CREATE TABLE sample_cycles(cycle_id INTEGER PRIMARY KEY AUTOINCREMENT, collected_at INTEGER NOT NULL UNIQUE, monotonic_started REAL NOT NULL, monotonic_finished REAL NOT NULL, duration_seconds REAL NOT NULL, success INTEGER NOT NULL, overrun INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE entities(entity_pk INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, display_name TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL, UNIQUE(entity_type,entity_id));
            CREATE TABLE tunnels(tunnel_id TEXT PRIMARY KEY, entity_pk INTEGER REFERENCES entities(entity_pk) ON DELETE SET NULL, side TEXT NOT NULL CHECK(side IN('iran','kharej')), tunnel_number INTEGER NOT NULL, service_name TEXT NOT NULL UNIQUE, env_path TEXT NOT NULL, listen_ports_json TEXT NOT NULL DEFAULT '[]', target_ports_json TEXT NOT NULL DEFAULT '[]', updated_at INTEGER NOT NULL, UNIQUE(side,tunnel_number));
            CREATE TABLE metric_samples(sample_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL REFERENCES sample_cycles(cycle_id) ON DELETE CASCADE, tunnel_id TEXT REFERENCES tunnels(tunnel_id) ON DELETE CASCADE, collected_at INTEGER NOT NULL, service_state INTEGER NOT NULL DEFAULT 0, service_substate INTEGER NOT NULL DEFAULT 0, restart_count INTEGER NOT NULL DEFAULT 0, listen_ports_total INTEGER NOT NULL DEFAULT 0, listen_ports_up INTEGER NOT NULL DEFAULT 0, configured_mappings_total INTEGER NOT NULL DEFAULT 0, rx_bytes INTEGER, tx_bytes INTEGER, UNIQUE(cycle_id,tunnel_id));
            CREATE TABLE metric_points(point_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL REFERENCES sample_cycles(cycle_id) ON DELETE CASCADE, entity_pk INTEGER NOT NULL REFERENCES entities(entity_pk) ON DELETE CASCADE, metric_name TEXT NOT NULL, ts INTEGER NOT NULL, numeric_value REAL, text_value TEXT, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), reset INTEGER NOT NULL DEFAULT 0, gap INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE metrics(sample_id INTEGER NOT NULL REFERENCES metric_samples(sample_id) ON DELETE CASCADE, scope TEXT NOT NULL, name TEXT NOT NULL, value REAL, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), labels_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE minute_rollups(entity_pk INTEGER NOT NULL REFERENCES entities(entity_pk) ON DELETE CASCADE, metric_name TEXT NOT NULL, minute_start INTEGER NOT NULL, samples INTEGER NOT NULL, expected_samples INTEGER NOT NULL, min_value REAL, avg_value REAL, max_value REAL, unavailable_count INTEGER NOT NULL, reset_count INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0, coverage REAL NOT NULL, unit TEXT NOT NULL, quality TEXT NOT NULL CHECK(quality IN('exact','derived','estimated','unavailable')), PRIMARY KEY(entity_pk,metric_name,minute_start));
            CREATE TABLE events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, severity TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'); CREATE TABLE collector_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO sample_cycles VALUES(1,900,0,1,1,1,0); INSERT INTO entities VALUES(1,'tunnel','iran-1','iran-1','{}',900);
            INSERT INTO metric_samples(sample_id,cycle_id,tunnel_id,collected_at) VALUES(1,1,NULL,900);
            INSERT INTO metric_points(cycle_id,entity_pk,metric_name,ts,numeric_value,unit,quality) VALUES(1,1,'listen_ports_up',900,1,'count','exact');
            INSERT INTO metrics VALUES(1,'tunnel.iran-1','listen_ports_up',2,'count','exact','{}');
            '''); c.commit(); c.close()
            conn=init_db(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM metric_points WHERE metric_name='listen_ports_up'").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entities WHERE entity_type='tunnel' AND entity_id='iran-1'").fetchone()[0],1)

class MonitoringFinalReviewTests(unittest.TestCase):
    def test_minute_rollups_time_index_owned_by_active_table_after_v2_migration(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3'); c=sqlite3.connect(db)
            c.executescript('''
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL); INSERT INTO schema_migrations VALUES(2,2);
            CREATE TABLE minute_rollups(scope TEXT NOT NULL, name TEXT NOT NULL, minute_start INTEGER NOT NULL, samples INTEGER NOT NULL, min_value REAL, avg_value REAL, max_value REAL, unavailable_count INTEGER NOT NULL, reset_count INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0, coverage REAL NOT NULL, unit TEXT NOT NULL, quality TEXT NOT NULL, PRIMARY KEY(scope,name,minute_start));
            CREATE INDEX idx_minute_rollups_time ON minute_rollups(minute_start);
            INSERT INTO minute_rollups VALUES('collector','x',60,1,1,1,1,0,0,0,1,'count','exact');
            '''); c.commit(); c.close()
            conn=init_db(db)
            rows=conn.execute("SELECT tbl_name FROM sqlite_master WHERE type='index' AND name='idx_minute_rollups_time'").fetchall()
            self.assertEqual(rows, [('minute_rollups',)])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM minute_rollups_archive').fetchone()[0],1)

    def test_no_duplicate_unique_indexes_for_metric_points_or_entities(self):
        with tempfile.TemporaryDirectory() as td:
            conn=init_db(str(Path(td)/'m.sqlite3'))
            def matching_unique(table, cols):
                count=0
                for row in conn.execute(f'PRAGMA index_list({table})'):
                    if row[2] and tuple(r[2] for r in conn.execute(f'PRAGMA index_info({row[1]})')) == cols:
                        count += 1
                return count
            self.assertEqual(matching_unique('metric_points', ('cycle_id','entity_pk','metric_name')),1)
            self.assertEqual(matching_unique('entities', ('entity_type','entity_id')),1)

    def test_deadline_missed_boundary_cases(self):
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'m.sqlite3'); init_db(db).close()
            cases=((900,5.000,0,0,0.0),(901,5.001,1,1,0.001),(902,10.000,1,1,5.0),(903,10.001,1,2,5.001),(904,12.000,1,2,7.0))
            for ts,finish,overrun,missed,seconds in cases:
                conn=open_runtime_database(db); conn.execute('BEGIN IMMEDIATE'); _cycle(conn,ts,0,finish,finish,True,False); conn.commit(); conn.close()
                record_cycle_overrun(db,ts,finish,0.0,5.0)
                row=open_runtime_database(db).execute('SELECT overrun,missed_deadlines,overrun_seconds FROM sample_cycles WHERE collected_at=?',(ts,)).fetchone()
                self.assertEqual(row[0],overrun); self.assertEqual(row[1],missed); self.assertAlmostEqual(row[2],seconds,places=3)
