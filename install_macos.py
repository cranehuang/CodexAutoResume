#!/usr/bin/env python3
"""Install/refresh the macOS launchd LaunchAgents for codex-auto-resume.

systemd has no macOS equivalent; launchd needs absolute paths inside a plist
(it does not expand ~ or $PATH), so the plists are generated at install time.
The generated jobs mirror the two systemd units:

  com.codex-auto-resume.guardian  -> guardian.py --daemon --interval 120 --send
  com.codex-auto-resume.web       -> web.py --host 127.0.0.1 --port 8787

Stdlib only, same as the rest of the project.
"""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

GUARDIAN_LABEL = 'com.codex-auto-resume.guardian'
WEB_LABEL = 'com.codex-auto-resume.web'
DEFAULT_INTERVAL = 120
DEFAULT_PORT = 8787
MIN_PYTHON = (3, 8)


def log_dir(home):
    return Path(home) / 'Library/Logs/codex-auto-resume'


def agents_dir(home):
    return Path(home) / 'Library/LaunchAgents'


def base_env():
    """launchd gives jobs a minimal environment; keep what the scripts need."""
    env = {'PYTHONUNBUFFERED': '1'}
    codex_home = os.environ.get('CODEX_HOME')
    if codex_home:
        env['CODEX_HOME'] = codex_home
    return env


def _check_absolute(name, value):
    if not os.path.isabs(str(value)):
        raise ValueError(f'{name} must be an absolute path: {value}')
    return str(value)


def build_jobs(home, install_dir, python, interval=DEFAULT_INTERVAL,
               port=DEFAULT_PORT, env=None):
    """Return [(label, plist_path, job_dict)] without touching the filesystem."""
    home = Path(_check_absolute('home', home))
    install_dir = Path(_check_absolute('install_dir', install_dir))
    python = _check_absolute('python', python)
    if int(interval) < 15:
        raise ValueError('interval must be at least 15 seconds')

    environment = base_env()
    environment.update(env or {})
    logs = log_dir(home)

    def job(label, script, args, throttle):
        return {
            'Label': label,
            'ProgramArguments': [python, str(install_dir / script), *args],
            'WorkingDirectory': str(install_dir),
            'EnvironmentVariables': dict(environment),
            'RunAtLoad': True,
            'KeepAlive': True,
            'ThrottleInterval': throttle,
            'Umask': 0o077,
            'StandardOutPath': str(logs / f'{script[:-3]}.log'),
            'StandardErrorPath': str(logs / f'{script[:-3]}.log'),
        }

    guardian = job(GUARDIAN_LABEL, 'guardian.py',
                   ['--daemon', '--interval', str(int(interval)), '--send'], 30)
    web = job(WEB_LABEL, 'web.py',
              ['--host', '127.0.0.1', '--port', str(int(port))], 10)
    return [(label, agents_dir(home) / f'{label}.plist', data)
            for label, data in ((GUARDIAN_LABEL, guardian), (WEB_LABEL, web))]


def write_plist(path, job):
    """Atomic 0644 write; launchd refuses group/world-writable plists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'wb') as fh:
        plistlib.dump(job, fh, sort_keys=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def write_plists(jobs):
    written = []
    for label, path, job in jobs:
        write_plist(path, job)
        written.append((label, path))
    return written


def check_python(python):
    """Fail early on stub/too-old interpreters: launchd failures are otherwise quiet."""
    try:
        res = subprocess.run([python, '-c', 'import sys; print("%d.%d" % sys.version_info[:2])'],
                             capture_output=True, text=True, timeout=20)
    except OSError as exc:
        raise SystemExit(f'cannot execute python interpreter {python}: {exc}')
    if res.returncode != 0:
        raise SystemExit(f'python interpreter {python} is not usable: '
                         f'{(res.stderr or res.stdout).strip()}')
    version = (res.stdout or '').strip()
    try:
        major, minor = (int(x) for x in version.split('.')[:2])
    except ValueError:
        raise SystemExit(f'cannot parse python version from {python}: {version!r}')
    if (major, minor) < MIN_PYTHON:
        raise SystemExit(f'python {version} at {python} is too old; need '
                         f'{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+')


def default_python():
    """Keep the PATH symlink (e.g. /opt/homebrew/bin/python3), not the versioned Cellar path."""
    explicit = os.environ.get('CODEX_GUARDIAN_PYTHON')
    found = explicit or shutil.which('python3') or sys.executable
    return os.path.abspath(found)


def launchctl(*args):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True)


def reload_job(label, plist_path):
    """bootout replaces a running job, so updating a plist takes effect immediately."""
    domain = f'gui/{os.getuid()}'
    launchctl('bootout', f'{domain}/{label}')
    res = launchctl('bootstrap', domain, str(plist_path))
    if res.returncode != 0:
        # Older macOS releases only support the deprecated load command.
        res = launchctl('load', '-w', str(plist_path))
        if res.returncode != 0:
            raise RuntimeError(f'cannot load {label}: {(res.stderr or res.stdout).strip()}')
    launchctl('enable', f'{domain}/{label}')


def job_state(label):
    return launchctl('print', f'gui/{os.getuid()}/{label}')


def main():
    p = argparse.ArgumentParser(description='Install macOS launchd agents for codex-auto-resume')
    p.add_argument('--install-dir', default=str(Path.home() / '.local/share/codex-auto-resume'))
    p.add_argument('--python', default=None, help='interpreter path to store in the plist')
    p.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    p.add_argument('--port', type=int, default=DEFAULT_PORT)
    p.add_argument('--no-load', action='store_true', help='write plists only, do not call launchctl')
    args = p.parse_args()

    if sys.platform != 'darwin':
        raise SystemExit('install_macos.py only runs on macOS')
    install_dir = Path(os.path.abspath(os.path.expanduser(args.install_dir)))
    if not (install_dir / 'guardian.py').is_file() or not (install_dir / 'web.py').is_file():
        raise SystemExit(f'{install_dir} does not look like an install dir; run ./install.sh')
    python = os.path.abspath(os.path.expanduser(args.python)) if args.python else default_python()
    check_python(python)

    jobs = build_jobs(Path.home(), install_dir, python,
                      interval=args.interval, port=args.port)
    log_dir(Path.home()).mkdir(parents=True, exist_ok=True)
    for label, path in write_plists(jobs):
        print(f'plist: {path}')
        if not args.no_load:
            reload_job(label, path)
            state = job_state(label)
            print(f'launchd: {label} {"loaded" if state.returncode == 0 else "NOT loaded"}')
            if state.returncode != 0:
                print(f'  {(state.stderr or state.stdout).strip()}', file=sys.stderr)
    print(f'logs: {log_dir(Path.home())}')
    print(f'python: {python}')


if __name__ == '__main__':
    main()
