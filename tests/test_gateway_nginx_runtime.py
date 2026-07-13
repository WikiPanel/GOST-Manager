from __future__ import annotations
import tempfile, unittest, os
from pathlib import Path
from dataclasses import replace
from typing import Sequence
from gateway.models import Strategy
from gateway.nginx_paths import NginxPaths, SERVICE_NAME
from gateway.nginx_render import render_config, render_unit, select_desired_nginx, parse_manifest
from gateway.nginx_apply import NginxGatewayManager, NginxRunner
from gateway.runtime_inspection import CommandResult
from gateway.runtime_models import Credentials
from gateway.runtime_paths import RuntimePaths
from gateway.secrets import SecretStore
from gateway.serialization import serialize_shared, serialize_node
from test_gateway_support import TemporaryStore, make_pair, add_secondary

class FakeNginxSystem:
    def __init__(self):
        self.commands=[]; self.active=False; self.pid=9000; self.fail_test=False
    def runner(self, argv:Sequence[str])->CommandResult:
        c=tuple(argv); self.commands.append(c)
        if c and c[0].endswith('nginx') and '-t' in c:
            return CommandResult(1 if self.fail_test else 0,'','bad')
        if c[:3]==('systemctl','is-active','--quiet'):
            return CommandResult(0 if self.active else 3)
        if c[:3]==('systemctl','show',SERVICE_NAME):
            return CommandResult(0,str(self.pid)+'\n')
        if c[:2]==('systemctl','start'):
            self.active=True; return CommandResult(0)
        if c[:2]==('systemctl','stop'):
            self.active=False; return CommandResult(0)
        return CommandResult(0)

class NginxRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.ts=TemporaryStore(); self.store=self.ts.store(); self.replace_pair(make_pair(gateway_enabled=True,route_enabled=True))
        rp=RuntimePaths.from_values(secret_dir=self.root/'secrets', generated_dir=self.root/'generated'/'gateway', runtime_backup_dir=self.root/'backups', runtime_lock_file=self.root/'runtime.lock', systemd_dir=self.root/'systemd', runner_path=self.root/'runner', gost_bin=self.root/'gost')
        for d in (rp.secret_dir,rp.generated_dir,rp.runtime_backup_dir,rp.systemd_dir): d.mkdir(parents=True, exist_ok=True); os.chmod(d,0o700)
        (self.root/'runner').write_text('#!/bin/sh\n'); (self.root/'gost').write_text('#!/bin/sh\n'); os.chmod(self.root/'runner',0o755); os.chmod(self.root/'gost',0o755)
        self.secrets=SecretStore(rp); self.secrets.set('secret-ee-primary', Credentials('user','pass'))
        self.np=NginxPaths.from_values(nginx_bin=self.root/'nginx', generated_dir=self.root/'generated'/'gateway'/'nginx', backup_dir=self.root/'nginx-backups', lock_file=self.root/'nginx.lock', systemd_dir=self.root/'systemd')
        (self.root/'nginx').write_text('#!/bin/sh\n'); os.chmod(self.root/'nginx',0o755)
    def replace_pair(self, pair):
        self.store.paths.state_file.write_bytes(serialize_shared(pair.shared))
        self.store.paths.node_file.write_bytes(serialize_node(pair.node))
    def pair(self):
        return self.store.load_pair()
    def tearDown(self): self.ts.close(); self.tmp.cleanup()
    def test_render_exact_host_path_websocket_status_and_no_etc_nginx(self):
        desired=select_desired_nginx(self.pair(), self.secrets)
        text=render_config(desired,self.np).decode()
        self.assertIn('server_name gateway.example.org;', text)
        self.assertIn('location = /ee1/api/v1 {', text)
        self.assertIn('proxy_http_version 1.1;', text)
        self.assertIn('proxy_set_header Host $host;', text)
        self.assertIn('proxy_pass http://route_estonia$request_uri;', text)
        self.assertIn('return 444;', text); self.assertIn('stub_status;', text)
        self.assertNotIn('/etc/nginx', text)
    def test_active_passive_marks_backup_and_active_active_does_not(self):
        p=add_secondary(make_pair(gateway_enabled=True,route_enabled=True))
        self.replace_pair(p); self.secrets.set('secret-de-backup', Credentials('user2','pass2'))
        text=render_config(select_desired_nginx(self.pair(), self.secrets), self.np).decode()
        self.assertIn('127.0.0.1:18082 max_fails=1 fail_timeout=2s backup;', text)
        p=replace(p, shared=replace(p.shared, routes=(replace(p.shared.routes[0], strategy=Strategy.ACTIVE_ACTIVE),)))
        self.replace_pair(p)
        text=render_config(select_desired_nginx(self.pair(), self.secrets), self.np).decode()
        self.assertNotIn('backup;', text)
    def test_apply_first_start_reload_noop_and_rollback(self):
        sys=FakeNginxSystem(); mgr=NginxGatewayManager(self.store,self.secrets,self.np,runner=NginxRunner(self.np,sys.runner))
        r=mgr.apply(); self.assertTrue(r.started); self.assertTrue(sys.active)
        r=mgr.apply(); self.assertFalse(r.reloaded); self.assertEqual(r.plan.action,'no-op')
        self.replace_pair(replace(self.pair(), shared=replace(self.pair().shared, revision=2)))
        r=mgr.apply(); self.assertTrue(r.reloaded); self.assertEqual(sys.pid,9000)
        before=self.np.config_file.read_bytes(); sys.fail_test=True
        with self.assertRaises(Exception): mgr.apply()
        self.assertEqual(self.np.config_file.read_bytes(), before)
    def test_unit_service_identity(self):
        text=render_unit(self.np).decode()
        self.assertIn('ExecStartPre='+str(self.np.nginx_bin)+' -t -c '+str(self.np.config_file), text)
        self.assertIn('LimitNOFILE=200000', text)

if __name__=='__main__': unittest.main()
