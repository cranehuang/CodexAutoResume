import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest

import install_macos
import probe_transport


class LaunchdAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.install = self.home / 'app'
        (self.install / 'webui').mkdir(parents=True)
        (self.install / 'guardian.py').touch()
        (self.install / 'web.py').touch()
        self.python = '/usr/bin/python3'

    def tearDown(self):
        self.tmp.cleanup()

    def jobs(self, **kwargs):
        return install_macos.build_jobs(self.home, self.install, self.python, **kwargs)

    def test_jobs_mirror_systemd_units(self):
        jobs = {label: (path, job) for label, path, job in self.jobs()}
        self.assertEqual(set(jobs), {install_macos.GUARDIAN_LABEL, install_macos.WEB_LABEL})

        path, guardian = jobs[install_macos.GUARDIAN_LABEL]
        self.assertEqual(path, self.home / 'Library/LaunchAgents' / f'{install_macos.GUARDIAN_LABEL}.plist')
        self.assertEqual(guardian['ProgramArguments'],
                         [self.python, str(self.install / 'guardian.py'),
                          '--daemon', '--interval', '120', '--send'])
        self.assertTrue(guardian['RunAtLoad'])
        self.assertTrue(guardian['KeepAlive'])
        self.assertEqual(guardian['Umask'], 0o077)
        self.assertEqual(guardian['WorkingDirectory'], str(self.install))
        log = self.home / 'Library/Logs/codex-auto-resume/guardian.log'
        self.assertEqual(guardian['StandardOutPath'], str(log))
        self.assertEqual(guardian['StandardErrorPath'], str(log))
        self.assertEqual(guardian['EnvironmentVariables']['PYTHONUNBUFFERED'], '1')

        web = jobs[install_macos.WEB_LABEL][1]
        self.assertEqual(web['ProgramArguments'][2:], ['--host', '127.0.0.1', '--port', '8787'])
        self.assertEqual(web['StandardOutPath'],
                         str(self.home / 'Library/Logs/codex-auto-resume/web.log'))

    def test_plists_are_valid_and_readable_by_launchd(self):
        for label, path in install_macos.write_plists(self.jobs()):
            job = plistlib.loads(path.read_bytes())
            self.assertEqual(job['Label'], label)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
            args = job['ProgramArguments']
            self.assertTrue(os.path.isabs(args[0]))
            self.assertTrue(os.path.isabs(args[1]))
            # launchd does not expand ~ in plist values, so never write one.
            self.assertNotIn('~/', path.read_text())

    def test_custom_interval_port_and_env(self):
        jobs = {label: job for label, _, job in
                self.jobs(interval=300, port=9999, env={'CODEX_HOME': '/tmp/codex'})}
        self.assertIn('300', jobs[install_macos.GUARDIAN_LABEL]['ProgramArguments'])
        self.assertIn('9999', jobs[install_macos.WEB_LABEL]['ProgramArguments'])
        env = jobs[install_macos.GUARDIAN_LABEL]['EnvironmentVariables']
        self.assertEqual(env['CODEX_HOME'], '/tmp/codex')
        self.assertEqual(env['PYTHONUNBUFFERED'], '1')

    def test_relative_paths_and_tiny_interval_rejected(self):
        for override in ({'install_dir': 'relative'}, {'python': 'python3'}, {'interval': 5}):
            args = {'home': self.home, 'install_dir': self.install, 'python': self.python}
            args.update(override)
            with self.assertRaises(ValueError):
                install_macos.build_jobs(**args)

    def test_default_python_honours_env_override(self):
        old = os.environ.get('CODEX_GUARDIAN_PYTHON')
        os.environ['CODEX_GUARDIAN_PYTHON'] = '/opt/x/python3'
        try:
            self.assertEqual(install_macos.default_python(), '/opt/x/python3')
        finally:
            if old is None:
                os.environ.pop('CODEX_GUARDIAN_PYTHON', None)
            else:
                os.environ['CODEX_GUARDIAN_PYTHON'] = old

    def test_check_python(self):
        install_macos.check_python(sys.executable)
        with self.assertRaises(SystemExit):
            install_macos.check_python('/definitely/not/python3')


class SocketPathLimitTests(unittest.TestCase):
    def test_normal_path_accepted(self):
        probe_transport.check_socket_path('/tmp/codex.sock')

    def test_overlong_path_rejected_with_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            probe_transport.check_socket_path('/tmp/' + 'x' * 120 + '.sock')
        self.assertIn('too long', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
