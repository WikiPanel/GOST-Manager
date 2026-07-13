"""Plan and apply for the dedicated NGINX Gateway service."""
from __future__ import annotations
import os, tempfile
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Sequence
from gateway.errors import OperationalError, ValidationError
from gateway.nginx_paths import NginxPaths, SERVICE_NAME
from gateway.nginx_render import NginxManifest, render_config, render_manifest, render_unit, select_desired_nginx, sha256
from gateway.runtime_store import RuntimeStore
from gateway.runtime_paths import RuntimePaths
from gateway.runtime_inspection import CommandResult, run_command, RuntimeInspector
from gateway.secrets import SecretStore
from gateway.store import GatewayStateStore
from gateway.paths import ensure_private_directory, reject_symlink_components

@dataclass(frozen=True)
class NginxPlan:
    action:str; reason:str; changed:bool; route_count:int; backend_count:int
@dataclass(frozen=True)
class NginxApplyResult:
    plan:NginxPlan; reloaded:bool; started:bool; stopped:bool; changed:bool

class NginxRunner:
    def __init__(self, paths:NginxPaths, runner:Callable[[Sequence[str]],CommandResult]=run_command):
        self.paths=paths; self.runner=runner
    def test_config(self, path:Path|None=None)->None:
        p=path or self.paths.config_file
        r=self.runner((str(self.paths.nginx_bin),'-t','-c',str(p),'-p',str(self.paths.generated_dir)))
        if r.returncode!=0: raise ValidationError('dedicated NGINX candidate failed validation')
    def systemctl(self,*args:str)->None:
        r=self.runner(('systemctl',*args))
        if r.returncode!=0: raise OperationalError('dedicated NGINX service operation failed')
    def service_active(self)->bool:
        r=self.runner(('systemctl','is-active','--quiet',SERVICE_NAME)); return r.returncode==0
    def master_pid(self)->int|None:
        r=self.runner(('systemctl','show',SERVICE_NAME,'--property=MainPID','--value'))
        try: pid=int(r.stdout.strip() or '0')
        except ValueError: pid=0
        return pid if r.returncode==0 and pid>0 else None

def _write_tmp(dir:Path, name:str, data:bytes)->Path:
    ensure_private_directory(dir)
    fd,p=tempfile.mkstemp(prefix='candidate-', suffix='-'+name, dir=dir)
    try:
        os.write(fd,data); os.fsync(fd)
    finally: os.close(fd)
    return Path(p)

class NginxGatewayManager:
    def __init__(self, state_store:GatewayStateStore, secret_store:SecretStore, paths:NginxPaths, *, runner:NginxRunner|None=None):
        self.state_store=state_store; self.secret_store=secret_store; self.paths=paths; self.runner=runner or NginxRunner(paths); self.store=RuntimeStore(RuntimePaths.from_values(generated_dir=str(paths.generated_dir.parent), runtime_backup_dir=str(paths.backup_dir), systemd_dir=str(paths.systemd_dir)))
    def _material(self):
        with self.state_store.locked_pair() as pair:
            with self.secret_store.lock():
                desired=select_desired_nginx(pair,self.secret_store)
                cfg=render_config(desired,self.paths)
                unit=render_unit(self.paths)
                backends=sum(len(r.upstreams) for r in desired.routes)
                manifest=NginxManifest(SERVICE_NAME,str(self.paths.nginx_bin),str(self.paths.config_file),sha256(cfg),desired.enabled,pair.shared.revision,pair.node.revision,len(desired.routes),backends)
                return desired,cfg,unit,render_manifest(manifest),manifest
    def plan(self)->NginxPlan:
        desired,cfg,unit,manifest,m=self._material()
        if not desired.enabled:
            return NginxPlan('stop','gateway disabled', True, m.route_count, m.backend_count)
        changed = self.paths.config_file.read_bytes()!=cfg if self.paths.config_file.exists() else True
        changed = changed or (self.paths.unit_file.read_bytes()!=unit if self.paths.unit_file.exists() else True)
        if self.paths.manifest_file.exists():
            changed = changed or self.paths.manifest_file.read_bytes()!=manifest
        else: changed=True
        return NginxPlan('apply' if changed else 'no-op', 'effective config changed' if changed else 'metadata-only/no-op', changed, m.route_count, m.backend_count)
    def apply(self)->NginxApplyResult:
        plan=self.plan(); desired,cfg,unit,manifest,_=self._material()
        for d in (self.paths.generated_dir,self.paths.backup_dir): ensure_private_directory(d)
        reject_symlink_components(self.paths.systemd_dir)
        if not desired.enabled:
            if self.runner.service_active(): self.runner.systemctl('stop',SERVICE_NAME); stopped=True
            else: stopped=False
            return NginxApplyResult(plan,False,False,stopped,stopped)
        candidate=_write_tmp(self.paths.generated_dir,'nginx.conf',cfg)
        snapshots=self.store.snapshot({self.paths.config_file,self.paths.manifest_file,self.paths.unit_file})
        backup=self.store.create_backup(snapshots)
        try:
            self.runner.test_config(candidate)
            self.store.write_atomic(self.paths.config_file,cfg,0o600)
            self.store.write_atomic(self.paths.unit_file,unit,0o644)
            self.runner.test_config(self.paths.config_file)
            self.runner.systemctl('daemon-reload')
            before=self.runner.master_pid()
            active=self.runner.service_active()
            if active and plan.changed:
                self.runner.systemctl('reload',SERVICE_NAME); reloaded=True; started=False
            elif not active:
                self.runner.systemctl('enable',SERVICE_NAME); self.runner.systemctl('start',SERVICE_NAME); started=True; reloaded=False
            else: started=False; reloaded=False
            after=self.runner.master_pid()
            if before and after and before!=after and reloaded: raise OperationalError('dedicated NGINX reload changed master PID')
            self.store.write_atomic(self.paths.manifest_file,manifest,0o600)
            self.store.remove_backup(backup)
            return NginxApplyResult(plan,reloaded,started,False,plan.changed or started)
        except Exception:
            self.store.restore(snapshots)
            try: self.runner.systemctl('daemon-reload')
            except Exception: pass
            raise
        finally:
            try: candidate.unlink()
            except FileNotFoundError: pass
