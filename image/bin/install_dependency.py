#!/usr/bin/env python3
"""Install-time download verification; credentials and user state are absent."""
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.request

name, destination = sys.argv[1:]
if name not in ('chrome', 'cloudflared'):
    raise SystemExit('Unsupported dependency')
package = json.loads(Path('/opt/codexpp/dependencies.json').read_text())[name]
output = Path(destination)
checksum = hashlib.sha256()
deadline = time.monotonic() + 240
try:
    with urllib.request.urlopen(package['url'], timeout=30) as response, output.open('xb') as stream:
        while data := response.read(1024 * 1024):
            if time.monotonic() > deadline or stream.tell() + len(data) > 250 * 1024 * 1024:
                raise ValueError('Dependency download limit exceeded')
            checksum.update(data)
            stream.write(data)
    if checksum.hexdigest() != package['sha256']:
        raise ValueError('Dependency checksum mismatch')
except BaseException:
    output.unlink(missing_ok=True)
    raise
