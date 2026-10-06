#!/usr/bin/env python3
"""Fetch public release metadata and hash exact packages; no account access."""
import hashlib
import json
from pathlib import Path
import urllib.request
import gzip

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    'cloudflared': 'https://github.com/cloudflare/cloudflared/releases/download/2026.9.3/cloudflared-linux-amd64.deb',
    'chrome': 'https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_154.0.8037.97-1_amd64.deb',
}


def main():
    dependencies = {'platform': 'linux/amd64', 'base_image':
        'node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c',
        'codex_version': '0.160.0'}
    for name, url in PACKAGES.items():
        if name == 'cloudflared':
            metadata = json.load(urllib.request.urlopen(
                'https://api.github.com/repos/cloudflare/cloudflared/releases/tags/2026.9.3', timeout=30))
            asset = next(item for item in metadata['assets'] if item['name'] == 'cloudflared-linux-amd64.deb')
            checksum = asset.get('digest', '').removeprefix('sha256:')
        else:
            with urllib.request.urlopen('https://dl.google.com/linux/chrome/deb/dists/stable/main/binary-amd64/Packages.gz', timeout=30) as response:
                packages = gzip.decompress(response.read()).decode()
            fields = next(dict(line.split(': ', 1) for line in stanza.splitlines() if ': ' in line)
                          for stanza in packages.split('\n\n')
                          if 'Package: google-chrome-stable\n' in stanza
                          and 'Version: 154.0.8037.97-1\n' in stanza)
            checksum = fields['SHA256']
            if not url.endswith(fields['Filename']):
                raise ValueError('Chrome release filename differs from official metadata')
        if len(checksum) != 64 or any(character not in '0123456789abcdef' for character in checksum):
            raise ValueError('Official SHA256 is unavailable; refusing an unverified package')
        dependencies[name] = {'url': url, 'sha256': checksum}
        print(name + ' exact release checksum obtained', flush=True)
    (ROOT / 'image' / 'dependencies.json').write_text(json.dumps(dependencies, indent=2) + '\n')


if __name__ == '__main__':
    main()
