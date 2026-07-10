#!/usr/bin/env python3
import os, sqlite3, tempfile, threading, time, unittest
from pathlib import Path

from monitoring.gost_monitoring import (
    CREATE_SCHEMA, RAW_RETENTION_SECONDS, ROLLUP_RETENTION_SECONDS, Metric,
    MetricSample, Tunnel, apply_retention, collect_once, collect_host_metrics, collect_sample,
    counter_delta, discover_tunnels, init_db, insert_metric, insert_sample, listener_quality,
    parse_ss_listeners, parse_systemd_properties, quality_worst,
    rollup_completed_minutes, scheduler_ticks, tunnel_from_env, upsert_tunnel,
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
            self.assertEqual(conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 3)
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
            self.assertEqual(conn.execute('SELECT samples,avg_value,expected_samples FROM minute_rollups WHERE minute_start=60').fetchone(), (2,3.0,12))
            old=10_000_000-RAW_RETENTION_SECONDS-1; insert_sample(conn,MetricSample('iran-1',old,1,1,0,1,1,1)); conn.execute("INSERT OR REPLACE INTO minute_rollups(entity_pk,metric_name,minute_start,samples,expected_samples,unavailable_count,coverage,unit,quality) VALUES(1,'b',?,?,?,?,?,?,?)", (10_000_000-ROLLUP_RETENTION_SECONDS-60,1,12,0,1,'x','exact'))
            apply_retention(conn,10_000_000)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM metric_samples WHERE collected_at=?',(old,)).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM minute_rollups WHERE minute_start<?',(10_000_000-ROLLUP_RETENTION_SECONDS,)).fetchone()[0],0)

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
            self.assertEqual(conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 3)

    def test_once_cli_fails_when_collection_fails(self):
        import monitoring.gost_monitoring as gm
        with tempfile.TemporaryDirectory() as td:
            Path(td,'iran-1.env').write_text('MAPPINGS=80:80\n',encoding='utf-8')
            old = gm.collect_once
            try:
                gm.collect_once = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))
                self.assertEqual(gm.main(['--db', str(Path(td)/'m.sqlite3'), '--env-dir', td, '--once']), 1)
            finally:
                gm.collect_once = old

if __name__ == '__main__': unittest.main()
