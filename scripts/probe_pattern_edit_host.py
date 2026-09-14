"""Trusted P0 prerequisite probe, NOT the P3 candidate sandbox launcher.

Run from the repository with .venv/bin/python scripts/probe_pattern_edit_host.py.
No generated code, account access, credentials, or network requests are used.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    results = {'tools': {name: shutil.which(name) for name in ('bwrap', 'Xvfb', 'gs', 'prlimit')},
               'cgroup_v2': Path('/sys/fs/cgroup/cgroup.controllers').exists(),
               'cgroup_root_writable': os.access('/sys/fs/cgroup', os.W_OK)}
    # Mount only runtime libraries. Neither workspace nor home is visible.
    command = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--clearenv',
               '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
               '--ro-bind', '/lib64', '/lib64', '--proc', '/proc', '--dev', '/dev',
               '--tmpfs', '/tmp', '--setenv', 'P0_INSIDE', '1',
               '/usr/bin/python3', '-c',
               'import os,socket; from pathlib import Path; '
               'assert not Path("/home").exists(); '
               'assert "P0_OUTSIDE" not in os.environ; '
               'assert set(os.listdir("/sys")) == set() if Path("/sys").exists() else True; '
               's=socket.socket(); assert s.connect_ex(("127.0.0.1",9)) != 0; '
               'Path("/tmp/probe").write_text("ok"); '
               'assert not os.access("/usr",os.W_OK); print("isolated mounts/env/network namespace: passed")']
    for name, cmd in [('bubblewrap', command), ('canvas', ['xvfb-run', '-a', sys.executable, '-c',
        'import tkinter as tk; from PIL import Image; import io; '
        'r=tk.Tk(); c=tk.Canvas(r,width=100,height=80); c.pack(); '
        'c.create_line(0,0,80,60); r.update(); '
        'im=Image.open(io.BytesIO(c.postscript(colormode="color").encode())); '
        'im.load(); assert im.width>0; print("Canvas PostScript rasterized", im.size); r.destroy()'])]:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                               env={**os.environ, 'P0_OUTSIDE': 'test-sentinel'})
            results[name] = {'passed': p.returncode == 0, 'detail': (p.stdout + p.stderr).strip()}
        except (OSError, subprocess.TimeoutExpired) as exc:
            results[name] = {'passed': False, 'detail': type(exc).__name__}
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
