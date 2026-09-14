"""Fail-closed Linux candidate runner. Imports execute inside the same boundary."""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from core.pattern_edit_store import EditError, canonical, uid


class SandboxUnavailable(EditError):
    pass


class Cancelled(EditError):
    pass


def seccomp_file():
    """Deny network, namespace, kernel instrumentation and cross-process access."""
    try:
        lib = ctypes.CDLL('libseccomp.so.2')
        lib.seccomp_init.argtypes = [ctypes.c_uint32]
        lib.seccomp_init.restype = ctypes.c_void_p
        lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        lib.seccomp_rule_add.argtypes = [ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint]
        lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p,ctypes.c_int]
        lib.seccomp_release.argtypes = [ctypes.c_void_p]
        ctx = lib.seccomp_init(0x7fff0000)  # allow
        if not ctx:
            raise OSError('seccomp_init')
        stream = tempfile.TemporaryFile()
        try:
            for name in ('socket','socketpair','connect','bind','listen','accept','accept4','mount',
                         'umount2','pivot_root','setns','unshare','ptrace','process_vm_readv',
                         'process_vm_writev','bpf','perf_event_open','keyctl','add_key',
                         'request_key','open_by_handle_at','reboot','kexec_load'):
                nr = lib.seccomp_syscall_resolve_name(name.encode())
                if nr >= 0 and lib.seccomp_rule_add(ctx,0x50000 | errno.EPERM,nr,0) != 0:
                    raise OSError('seccomp_rule_add')
            if lib.seccomp_export_bpf(ctx,stream.fileno()) != 0:
                raise OSError('seccomp_export_bpf')
        finally:
            lib.seccomp_release(ctx)
        stream.seek(0)
        return stream
    except (OSError, AttributeError) as exc:
        raise SandboxUnavailable('Install libseccomp; candidate execution is unavailable') from exc


class CandidateRunner:
    def __init__(self, *, cgroup_root=None, python=None, timeout=30):
        from config import settings
        self.cgroup_root = cgroup_root or settings.pattern_edit_cgroup_root
        self.python = python or settings.pattern_edit_worker_python
        self.timeout = min(timeout, 60)

    def available(self):
        if not shutil.which('bwrap') or not shutil.which('prlimit'):
            raise SandboxUnavailable('Install Bubblewrap and util-linux prlimit')
        if not self.cgroup_root or not self.python:
            raise SandboxUnavailable('Configure PATTERN_EDIT_CGROUP_ROOT and PATTERN_EDIT_WORKER_PYTHON; see docs/pattern-edit-operation.md')
        root = Path(self.cgroup_root).resolve()
        if not root.is_relative_to(Path('/sys/fs/cgroup')) or not os.access(root,os.W_OK):
            raise SandboxUnavailable('A writable delegated cgroup v2 subtree is required')
        enabled = (root/'cgroup.subtree_control').read_text().split()
        if not {'pids','memory','cpu'}.issubset(enabled):
            raise SandboxUnavailable('Enable delegated pids, memory and cpu cgroup controllers')
        python = Path(self.python).absolute()
        if not python.is_file():
            raise SandboxUnavailable('Configured worker Python is missing')
        return root,python

    def run(self, files, request, cancel=None):
        root,python = self.available()
        group = root / ('pattern-edit-'+uid())
        group.mkdir()
        process = None
        try:
            for name,value in {'pids.max':'16','memory.max':str(1024**3),'memory.swap.max':'0',
                               'cpu.max':'100000 100000'}.items():
                (group/name).write_text(value)
                if (group/name).read_text().strip() != value:
                    raise SandboxUnavailable('Cgroup limits could not be verified')
            with tempfile.TemporaryDirectory(prefix='pattern-edit-') as scratch, seccomp_file() as seccomp:
                scratch = Path(scratch)
                inputs = scratch/'input'
                inputs.mkdir()
                from core.pattern_edit_store import safe_relative
                for name,data in files.items():
                    dest = inputs/str(safe_relative(name))
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    dest.write_bytes(data if isinstance(data,bytes) else data.encode())
                (inputs/'request.json').write_bytes(canonical(request))
                # A trusted bootstrap joins the cgroup BEFORE it execs bwrap.
                bootstrap = 'import os,sys; open(sys.argv[1],"w").write(str(os.getpid())); os.execvp(sys.argv[2],sys.argv[2:])'
                env_root = python.parent.parent
                args = [str(python),'-I','-c',bootstrap,str(group/'cgroup.procs'),
                        'bwrap','--unshare-all','--die-with-parent','--new-session','--clearenv',
                        '--ro-bind','/usr','/usr','--ro-bind','/lib','/lib','--ro-bind','/lib64','/lib64',
                        '--ro-bind',str(env_root),str(env_root),'--ro-bind',str(inputs),'/input',
                        '--proc','/proc','--dev','/dev','--tmpfs','/tmp','--chdir','/tmp',
                        '--setenv','HOME','/tmp','--setenv','MPLCONFIGDIR','/tmp/mpl',
                        '--setenv','WATCHLIST','FIXTURE','--setenv','TV_SCREENER','america',
                        '--setenv','TV_EXCHANGE','NASDAQ','--setenv','OPENBLAS_NUM_THREADS','1',
                        '--setenv','OMP_NUM_THREADS','1','--seccomp',str(seccomp.fileno()),
                        '/usr/bin/prlimit','--cpu=20','--fsize=16777216','--nofile=128',
                        str(python),'-B','/input/core/pattern_edit_evaluator.py']
                with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                    process = subprocess.Popen(args, stdout=output, stderr=errors, pass_fds=(seccomp.fileno(),),
                                               env={'PATH':'/usr/bin:/bin'})
                    deadline = time.monotonic()+self.timeout
                    while process.poll() is None:
                        if cancel is not None and cancel.is_set():
                            raise Cancelled('Job cancelled')
                        if time.monotonic()>deadline:
                            raise EditError('Candidate exceeded the execution deadline')
                        time.sleep(.05)
                    if process.returncode:
                        # Do not reflect arbitrary candidate stderr/secrets into a UI.
                        raise EditError(f'Candidate worker failed (exit {process.returncode})')
                    output.seek(0)
                    data = output.read(16*1024*1024+1)
                    if len(data)>16*1024*1024:
                        raise EditError('Candidate output exceeded limit')
                    try:
                        return json.loads(data)
                    except (ValueError,UnicodeDecodeError) as exc:
                        raise EditError('Candidate returned invalid output') from exc
        finally:
            try:
                (group/'cgroup.kill').write_text('1')
            finally:
                if process:
                    process.wait(timeout=5)
                for _ in range(20):
                    try:
                        group.rmdir()
                        break
                    except OSError:
                        time.sleep(.05)
