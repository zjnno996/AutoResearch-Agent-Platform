#!/usr/bin/env python3
"""Install pinned Linux x86_64 gateway tools after checking release SHA-256s."""
import hashlib
import os
from pathlib import Path
import platform
import tarfile
import tempfile
import urllib.request

TOOLS = {
    'caddy': (
        'https://github.com/caddyserver/caddy/releases/download/v2.11.7/caddy_2.11.7_linux_amd64.tar.gz',
        '727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b'),
    'cloudflared': (
        'https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-amd64',
        '1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872'),
}


def main():
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise SystemExit('Install Caddy and cloudflared for your platform and configure their binary paths.')
    target = Path(os.environ.get('EMNETS_TOOLS_BIN', str(Path.home()/'.local/bin')))
    target.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for name, (url, expected) in TOOLS.items():
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp)/'download'
            with opener.open(urllib.request.Request(url, headers={'User-Agent': 'EMNETS-RA-deployment'}), timeout=40) as response, archive.open('wb') as handle:
                total = 0
                while chunk := response.read(262144):
                    total += len(chunk)
                    if total > 128*1024*1024:
                        raise ValueError('Unexpected release size')
                    handle.write(chunk)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
                raise ValueError('Release checksum mismatch: '+name)
            if name == 'caddy':
                with tarfile.open(archive) as package:
                    member = package.getmember('caddy')
                    if not member.isfile():
                        raise ValueError('Unexpected Caddy archive member')
                    data = package.extractfile(member).read()
            else:
                data = archive.read_bytes()
            staged = target/('.'+name+'.install')
            staged.write_bytes(data)
            staged.chmod(0o755)
            staged.replace(target/name)
            print('Installed verified '+name+' at '+str(target/name))


if __name__ == '__main__':
    main()
