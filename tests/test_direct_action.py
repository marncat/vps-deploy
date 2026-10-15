import gzip
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DirectActionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='direct action ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.archive = self.root / 'release.tar.gz'
        self.archive.write_bytes(gzip.compress(b'release contents'))
        self.env = {
            **os.environ,
            'PATH': f'{self.root}:' + os.environ['PATH'],
            'RUNNER_TEMP': str(self.root),
            'DEPLOY_PROJECT': 'example-app',
            'ARCHIVE_PATH': str(self.archive),
            'GITHUB_RUN_ID': '1234',
            'GITHUB_RUN_ATTEMPT': '2',
            'GITHUB_SHA': 'abcdef012345' + '0' * 28,
            'GITHUB_OUTPUT': str(self.root / 'outputs'),
            'GITHUB_STEP_SUMMARY': str(self.root / 'summary'),
            'VPS_HOST': 'example.invalid',
            'VPS_PORT': '2222',
            'VPS_SSH_KEY': 'PRIVATE KEY FIXTURE',
            'VPS_KNOWN_HOSTS': 'HOST KEY FIXTURE',
            'MOCK_MODE': 'success',
            'MOCK_ROOT': str(self.root),
        }
        self.mock('ssh-keygen', '''
import os
from pathlib import Path
import stat
import sys
args = sys.argv[1:]
path = Path(args[args.index('-f') + 1])
assert stat.S_IMODE(path.stat().st_mode) == 0o600
assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
if '-y' in args:
    assert '-P' in args and args[args.index('-P') + 1] == ''
    with open(Path(os.environ['MOCK_ROOT']) / 'key_paths', 'a') as log:
        log.write(str(path.parent) + '\\n')
    sys.exit(1 if os.environ['MOCK_MODE'] == 'invalid-key' else 0)
assert args[args.index('-F') + 1] == f"[{os.environ['VPS_HOST']}]:2222"
sys.exit(1 if os.environ['MOCK_MODE'] == 'unknown-host' else 0)
''')
        self.mock('ssh', '''
import hashlib
import os
from pathlib import Path
import sys
args = sys.argv[1:]
for option in ('BatchMode=yes', 'ConnectTimeout=10', 'ServerAliveInterval=30',
               'ServerAliveCountMax=10', 'IdentitiesOnly=yes', 'StrictHostKeyChecking=yes'):
    assert option in args
assert args[-1] == 'deploy example-app 1234-2-abcdef012345'
assert args[-2] == 'deploy@' + os.environ['VPS_HOST']
assert args[args.index('-p') + 1] == '2222'
assert 'VPS_SSH_KEY' not in os.environ
assert 'VPS_KNOWN_HOSTS' not in os.environ
with open(Path(os.environ['MOCK_ROOT']) / 'ssh_calls', 'a') as log:
    log.write('called\\n')
digest = hashlib.sha256(sys.stdin.buffer.read()).hexdigest()
assert digest == os.environ['ARCHIVE_SHA256']
mode = os.environ['MOCK_MODE']
if mode == 'terminated':
    import signal
    os.kill(os.getppid(), signal.SIGTERM)
    sys.exit(0)
if mode == 'disconnected':
    sys.exit(255)
release = 'another-release' if mode == 'wrong-release' else '1234-2-abcdef012345'
if mode == 'wrong-digest':
    digest = '0' * 64
if mode != 'missing':
    response = f'deployment healthy: example-app {release} sha256={digest}'
    print(response + (' extra' if mode == 'extra-response' else ''))
sys.exit(255 if mode == 'success-then-disconnected' else 0)
''')

    def mock(self, name, source):
        path = self.root / name
        path.write_text('#!/usr/bin/env python3\n' + source)
        path.chmod(0o700)

    def run_script(self, name):
        result = subprocess.run(
            ['bash', str(ROOT / 'scripts' / name)], env=self.env,
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotIn('PRIVATE KEY FIXTURE', result.stdout + result.stderr)
        self.assertNotIn('HOST KEY FIXTURE', result.stdout + result.stderr)
        return result

    def prepare(self):
        result = self.run_script('prepare.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = dict(line.split('=', 1) for line in (self.root / 'outputs').read_text().splitlines())
        self.env.update(ARCHIVE_SHA256=outputs['archive_sha256'], RELEASE_ID=outputs['release_id'])
        return outputs

    def assert_clean(self):
        self.assertEqual(list(self.root.glob('vps-deploy.*')), [])

    def test_archive_with_spaces_and_run_identity(self):
        outputs = self.prepare()
        self.assertEqual(outputs['release_id'], '1234-2-abcdef012345')
        self.assertEqual(outputs['archive_sha256'], hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_reject_invalid_archives(self):
        for kind in ('missing', 'empty', 'corrupt', 'directory', 'symlink', 'wrong-name'):
            with self.subTest(kind=kind):
                bad = self.root / kind / 'release.tar.gz'
                bad.parent.mkdir()
                if kind == 'empty':
                    bad.touch()
                elif kind == 'corrupt':
                    bad.write_bytes(b'not gzip')
                elif kind == 'directory':
                    bad.mkdir()
                elif kind == 'symlink':
                    bad.symlink_to(self.archive)
                elif kind == 'wrong-name':
                    bad = bad.with_name('other.tar.gz')
                    bad.write_bytes(self.archive.read_bytes())
                self.env['ARCHIVE_PATH'] = str(bad)
                self.assertNotEqual(self.run_script('prepare.sh').returncode, 0)

    def test_reject_invalid_project_and_run_identity(self):
        for name, value in (('DEPLOY_PROJECT', 'app; id'), ('DEPLOY_PROJECT', '-app'),
                            ('GITHUB_RUN_ID', '123; id'), ('GITHUB_RUN_ATTEMPT', ''),
                            ('GITHUB_SHA', 'nope')):
            with self.subTest(name=name, value=value):
                previous = self.env[name]
                self.env[name] = value
                self.assertNotEqual(self.run_script('prepare.sh').returncode, 0)
                self.env[name] = previous

    def test_confirmed_release_and_unique_private_cleanup(self):
        self.prepare()
        for _ in range(2):
            result = self.run_script('deploy.sh')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_clean()
        paths = (self.root / 'key_paths').read_text().splitlines()
        self.assertEqual(len(set(paths)), 2)
        self.assertIn('### Deployment confirmed', (self.root / 'summary').read_text())

    def test_ipv6_host(self):
        self.prepare()
        self.env['VPS_HOST'] = '::1'
        result = self.run_script('deploy.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_clean()

    def test_termination_removes_private_files(self):
        self.prepare()
        self.env['MOCK_MODE'] = 'terminated'
        result = self.run_script('deploy.sh')
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assert_clean()

    def test_failure_response_and_disconnect_never_retry(self):
        self.prepare()
        for mode in ('missing', 'wrong-release', 'wrong-digest', 'extra-response',
                     'disconnected', 'success-then-disconnected'):
            with self.subTest(mode=mode):
                self.env['MOCK_MODE'] = mode
                (self.root / 'ssh_calls').unlink(missing_ok=True)
                result = self.run_script('deploy.sh')
                expected = 255 if 'disconnected' in mode else 1
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual((self.root / 'ssh_calls').read_text(), 'called\n')
                self.assertIn('Verify service and release before retrying', (self.root / 'summary').read_text())
                self.assert_clean()

    def test_key_and_host_validation_cleanup(self):
        self.prepare()
        for mode in ('invalid-key', 'unknown-host', 'crlf'):
            with self.subTest(mode=mode):
                self.env['MOCK_MODE'] = mode
                self.env['VPS_KNOWN_HOSTS'] = 'HOST KEY FIXTURE' + ('\r\n' if mode == 'crlf' else '')
                self.assertNotEqual(self.run_script('deploy.sh').returncode, 0)
                self.assertFalse((self.root / 'ssh_calls').exists())
                self.assert_clean()

    def test_reject_invalid_transport_inputs_before_ssh(self):
        self.prepare()
        for name, value in (('VPS_HOST', 'host; id'), ('VPS_HOST', '-option'),
                            ('VPS_HOST', 'deploy@host'), ('VPS_HOST', 'host\nother'),
                            ('VPS_PORT', '0'), ('VPS_PORT', '65536'), ('VPS_PORT', '22; id'),
                            ('VPS_SSH_KEY', ''), ('VPS_KNOWN_HOSTS', ''),
                            ('RELEASE_ID', 'release; id'), ('ARCHIVE_SHA256', 'bad')):
            with self.subTest(name=name, value=value):
                previous = self.env[name]
                self.env[name] = value
                self.assertNotEqual(self.run_script('deploy.sh').returncode, 0)
                self.env[name] = previous
                self.assertFalse((self.root / 'ssh_calls').exists())
                self.assert_clean()

    def test_archive_changed_after_preparation(self):
        self.prepare()
        self.archive.write_bytes(gzip.compress(b'changed release'))
        self.assertNotEqual(self.run_script('deploy.sh').returncode, 0)
        self.assertFalse((self.root / 'ssh_calls').exists())
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
