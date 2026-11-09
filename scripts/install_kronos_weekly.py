#!/usr/bin/env python3
"""Render/install only the new weekly Kronos user units; enable after rehearsal."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.kronos_weekly import atomic_text, source_fingerprint

UNIT = 'kronos-weekly-finetune'


def units(root: Path = ROOT) -> dict[str, str]:
    def quoted(value):
        # systemd specifiers expand even inside quotes.
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    service = f'''[Unit]
Description=Weekly shared US and PH Kronos fine-tuning

[Service]
Type=oneshot
WorkingDirectory={str(root).replace('%', '%%')}
Environment=TZ=Asia/Manila
Environment=PYTHONUNBUFFERED=1
Environment=KRONOS_DEVICE=cuda
ExecStart={quoted(root / '.venv-kronos/bin/python')} {quoted(root / 'scripts/kronos_weekly_finetune.py')}
TimeoutStartSec=12h
KillMode=control-group
Restart=no
'''
    timer = f'''[Unit]
Description=Saturday 06:00 PHT Kronos fine-tuning

[Timer]
OnCalendar=Sat *-*-* 06:00:00 Asia/Manila
AccuracySec=1s
RandomizedDelaySec=0
Persistent=false
Unit={UNIT}.service

[Install]
WantedBy=timers.target
'''
    return {f'{UNIT}.service': service, f'{UNIT}.timer': timer}


def verify_rehearsal(path: Path) -> None:
    manifest = json.loads(path.read_text())
    if manifest.get('status') != 'rehearsed' or not manifest.get('rehearsal') or manifest['config'].get('smoke'):
        raise ValueError('Enabling requires a successful full rehearsal, not a smoke run')
    if manifest['source_sha256'] != source_fingerprint(ROOT):
        raise ValueError('Source changed since rehearsal; repeat the full rehearsal')
    from scripts.kronos_weekly_finetune import configuration, parser
    if manifest['config'] != configuration(parser().parse_args([])):
        raise ValueError('Rehearsal configuration differs from the scheduled production defaults')
    if manifest.get('failures'):
        raise ValueError('Rehearsal evaluation failed')
    from core.kronos_provenance import checkpoint_provenance
    checkpoint_provenance(path.parent, None, required=True)
    from core.kronos_weekly_eval import acceptance
    metrics = manifest.get('evaluation', {})
    failures = acceptance(metrics.get('candidate', {}), metrics.get('base', {}))
    if failures:
        raise ValueError(f'Rehearsal metrics do not qualify: {failures}')
    if any(manifest.get('freshness', {}).get(m, {}).get('fresh_fraction', 0) < .95 for m in ('us', 'ph')):
        raise ValueError('Rehearsal history did not meet the 95% freshness requirement')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--install', action='store_true', help='Install units; otherwise print them')
    ap.add_argument('--enable', action='store_true', help='Enable timer after a full rehearsal')
    ap.add_argument('--rehearsal-manifest', type=Path)
    args = ap.parse_args(argv)
    try:
        if args.enable:
            if not args.install or not args.rehearsal_manifest:
                raise ValueError('--enable requires --install and --rehearsal-manifest')
            verify_rehearsal(args.rehearsal_manifest)
            linger = subprocess.check_output(['loginctl', 'show-user', str(os.getuid()), '-p', 'Linger', '--value'], text=True).strip()
            if linger != 'yes':
                raise ValueError('User lingering must be enabled on the training host before enabling the timer')
        rendered = units()
        if not args.install:
            for name, content in rendered.items():
                print(f'# {name}\n{content}')
            return 0
        with tempfile.TemporaryDirectory(prefix='kronos-units-') as temporary:
            paths = []
            for name, content in rendered.items():
                path = Path(temporary) / name
                path.write_text(content)
                paths.append(str(path))
            subprocess.run(['systemd-analyze', '--user', 'verify', *paths], check=True)
        destination = Path.home() / '.config' / 'systemd' / 'user'
        for name, content in rendered.items():
            atomic_text(destination / name, content)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        if args.enable:
            subprocess.run(['systemctl', '--user', 'enable', '--now', f'{UNIT}.timer'], check=True)
        print('Installed weekly Kronos units.' + (' Timer enabled.' if args.enable else ' Timer remains disabled.'))
        return 0
    except Exception as exc:
        print(f'Kronos timer installation failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
