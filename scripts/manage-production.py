#!/usr/bin/env python3
"""Manage an isolated production workspace without changing development runs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.local/production'
PRIVATE = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'emnets-ra'
PYTHON = ROOT / '.venv-web/bin/python'
CONFIG = STATE / 'supervisord.conf'


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text)
    temporary.chmod(0o600)
    temporary.replace(path)


def control(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run([str(PYTHON), '-m', 'supervisor.supervisorctl', '-c', str(CONFIG), *args],
                          text=True, capture_output=True, check=check)


def prepare(public: bool) -> None:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    (STATE/'logs').mkdir(exist_ok=True, mode=0o700)
    runs = Path(os.environ.get('EMNETS_PRODUCTION_RUNS_DIR', str(ROOT/'backend/runs/production'))).resolve()
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    access_path = PRIVATE/'production-access.json'
    if access_path.exists():
        access = json.loads(access_path.read_text())
    else:
        access = {'username': 'emnets', 'password': secrets.token_urlsafe(18)}
        private_write(access_path, json.dumps(access, indent=2)+'\n')
    sys.path.insert(0, str(ROOT/'backend/services'))
    from user_auth import register_user, login_user, load_users
    if access['username'] not in load_users(str(runs)):
        register_user(access['username'], access['password'], str(runs))
    if not login_user(access['username'], access['password'], str(runs)):
        raise SystemExit('Saved production credentials do not match this workspace; no password was changed.')
    private_write(PRIVATE/'production-login.txt',
                  'EMNETS-RA\nUsername: '+access['username']+'\nPassword: '+access['password']+'\n'
                  'Use this account for the HTTPS access prompt and application login.\n')
    caddy = Path(os.environ.get('EMNETS_CADDY_BIN', str(Path.home()/'.local/bin/caddy'))).resolve()
    cloudflared = Path(os.environ.get('EMNETS_CLOUDFLARED_BIN', str(Path.home()/'.local/bin/cloudflared'))).resolve()
    hashed = subprocess.run([str(caddy), 'hash-password'], input=access['password']+'\n',
                            text=True, capture_output=True, check=True).stdout.strip()
    values = {
        'EMNETS_CADDY_BIN': str(caddy), 'EMNETS_CLOUDFLARED_BIN': str(cloudflared),
        'EMNETS_WEB_DIST': str(ROOT/'frontend-web/dist'),
        'EMNETS_PRODUCTION_RUNS_DIR': str(runs),
        'EMNETS_REVIEW_HISTORY_DIR': str(runs/'review_history'),
        'EMNETS_REVIEW_CACHE_DIR': str(runs/'review_cache'),
        'EMNETS_EDGE_USER': access['username'], 'EMNETS_EDGE_PASSWORD_HASH': hashed,
        'EMNETS_GATEWAY_PORT': os.environ.get('EMNETS_GATEWAY_PORT', '8080'),
        'EMNETS_DIRECT_PORT': os.environ.get('EMNETS_DIRECT_PORT', '5910'),
        'EMNETS_PRODUCTION_AGENT_PORT': os.environ.get('EMNETS_PRODUCTION_AGENT_PORT', '9906'),
        'EMNETS_PRODUCTION_RESOURCE_PORT': os.environ.get('EMNETS_PRODUCTION_RESOURCE_PORT', '9905'),
        'EMNETS_PRODUCTION_REVIEW_PORT': os.environ.get('EMNETS_PRODUCTION_REVIEW_PORT', '9907'),
        'RESEARCHCLAW_SKIP_TORCH_INSTALL': '1',
    }
    for key in ('EMNETS_GATEWAY_PORT', 'EMNETS_DIRECT_PORT', 'EMNETS_PRODUCTION_AGENT_PORT',
                'EMNETS_PRODUCTION_RESOURCE_PORT', 'EMNETS_PRODUCTION_REVIEW_PORT'):
        if not values[key].isdigit() or not 1024 <= int(values[key]) <= 65535:
            raise SystemExit('Invalid production port: '+key)
    private_write(STATE/'services.env', ''.join(k+'='+shlex.quote(v)+'\n' for k,v in values.items()))
    config = ('[unix_http_server]\nfile='+str(STATE/'supervisor.sock')+'\nchmod=0700\n'
              '[supervisord]\npidfile='+str(STATE/'supervisord.pid')+'\n'
              'logfile='+str(STATE/'logs/supervisord.log')+'\nlogfile_maxbytes=10MB\n'
              '[rpcinterface:supervisor]\nsupervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface\n'
              '[supervisorctl]\nserverurl=unix://'+str(STATE/'supervisor.sock')+'\n')
    for priority,name in enumerate(('resources','research','review','gateway','tunnel'), 10):
        command = shlex.join(['bash',str(ROOT/'scripts/run-production-service.sh'),name])
        config += ('\n[program:'+name+']\ncommand='+command+'\ndirectory='+str(ROOT)+'\n'
                   'autostart='+str(name!='tunnel' or public).lower()+'\nautorestart=true\n'
                   'startsecs=2\nstartretries=5\nstopasgroup=true\nkillasgroup=true\n'
                   'stopwaitsecs=30\numask=0077\npriority='+str(priority)+'\n'
                   'stdout_logfile='+str(STATE/'logs'/f'{name}.log')+'\n'
                   'stdout_logfile_maxbytes=20MB\nstdout_logfile_backups=3\nredirect_stderr=true\n')
    private_write(CONFIG,config.replace(str(ROOT), str(ROOT).replace('%','%%')))
    print('Production account saved at '+str(PRIVATE/'production-login.txt'))


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['start','status','stop','url'])
    parser.add_argument('--public',action='store_true',help='Start a temporary Cloudflare HTTPS tunnel')
    args = parser.parse_args()
    if args.command=='start':
        if CONFIG.exists() and control('pid').returncode==0:
            print(control('status').stdout);return
        if not (ROOT/'frontend-web/dist/index.html').is_file():
            raise SystemExit('Build the frontend first: npm --prefix frontend-web run build')
        prepare(args.public)
        subprocess.run(['bash',str(ROOT/'scripts/run-production-service.sh'),'validate'],check=True)
        subprocess.run([str(PYTHON),'-m','supervisor.supervisord','-c',str(CONFIG)],check=True)
        print('Production supervisor started. Use status and url to inspect readiness.')
    elif args.command=='url':
        log=STATE/'logs/tunnel.log'
        urls=re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com',log.read_text() if log.exists() else '')
        if not urls or control('status','tunnel').returncode!=0:
            raise SystemExit('No running public tunnel; inspect .local/production/logs/tunnel.log')
        print(urls[-1])
    else:
        result=control('shutdown' if args.command=='stop' else 'status')
        print(result.stdout or result.stderr,end='')
        raise SystemExit(result.returncode)


if __name__=='__main__':
    main()
