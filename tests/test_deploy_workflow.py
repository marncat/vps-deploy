import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/deploy.yml'


def step_script(name):
    """Execute the actual inline Bash, without a YAML dependency in the toolkit."""
    step = WORKFLOW.read_text().split(f'      - name: {name}\n', 1)[1]
    step = step.split('\n      - name:', 1)[0]
    return textwrap.dedent(step.split('        run: |\n', 1)[1])


class DeployWorkflowTests(unittest.TestCase):
    def run_deployment(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'release.tar.gz'
            archive.write_bytes(b'validated archive fixture')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            summary = root / 'summary.md'
            ssh = root / 'ssh'
            ssh.write_text('''#!/usr/bin/env python3
import hashlib
import os
import sys
args = sys.argv[1:]
assert 'ServerAliveInterval=30' in args
assert 'ServerAliveCountMax=10' in args
assert 'StrictHostKeyChecking=yes' in args
assert args[-1] == 'deploy listen-again run-1-abc'
digest = hashlib.sha256(sys.stdin.buffer.read()).hexdigest()
assert digest == os.environ['ARCHIVE_SHA256']
mode = os.environ['MOCK_MODE']
if mode == 'disconnected':
    print('client_loop: send disconnect: Broken pipe', file=sys.stderr)
    sys.exit(255)
if mode == 'missing':
    print('Still preparing the service')
    sys.exit(0)
release = 'another-release' if mode == 'wrong-release' else 'run-1-abc'
if mode == 'wrong-digest':
    digest = '0' * 64
print(f'deployment healthy: listen-again {release} sha256={digest}')
sys.exit(255 if mode == 'success-then-disconnected' else 0)
''')
            ssh.chmod(0o700)
            env = {
                **os.environ,
                'PATH': f'{root}:' + os.environ['PATH'],
                'RUNNER_TEMP': str(root),
                'ARCHIVE_PATH': str(archive),
                'ARCHIVE_SHA256': digest,
                'GITHUB_STEP_SUMMARY': str(summary),
                'DEPLOY_PROJECT': 'listen-again',
                'RELEASE_ID': 'run-1-abc',
                'VPS_HOST': 'unused.invalid',
                'VPS_PORT': '22',
                'MOCK_MODE': mode,
            }
            result = subprocess.run(
                ['bash', '-c', step_script('Stream and activate release')],
                env=env, capture_output=True, text=True, timeout=10,
            )
            return result, summary.read_text() if summary.exists() else ''

    def test_confirmed_release(self):
        result, summary = self.run_deployment('success')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('### Deployment confirmed', summary)
        self.assertIn('run-1-abc', summary)

    def test_completion_response_must_match_release_and_archive(self):
        for mode in ('missing', 'wrong-release', 'wrong-digest'):
            with self.subTest(mode=mode):
                result, summary = self.run_deployment(mode)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('Deployment not confirmed', summary)

    def test_disconnect_is_not_success_even_after_completion_response(self):
        for mode in ('disconnected', 'success-then-disconnected'):
            with self.subTest(mode=mode):
                result, summary = self.run_deployment(mode)
                self.assertEqual(result.returncode, 255, result.stderr)
                self.assertIn('may still be running', summary)


if __name__ == '__main__':
    unittest.main()
