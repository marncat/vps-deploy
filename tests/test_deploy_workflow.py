import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/deploy.yml'


class CompatibilityWorkflowTests(unittest.TestCase):
    def test_transport_is_delegated_to_pinned_root_action(self):
        workflow = WORKFLOW.read_text()
        self.assertRegex(workflow, r'uses: marncat/vps-deploy@[0-9a-f]{40}\n')
        self.assertNotIn('ssh-keygen', workflow)
        self.assertNotIn('        run: ssh', workflow)
        self.assertNotIn('    env:', workflow)
        self.assertIn('timeout-minutes: 60', workflow)
        self.assertIn('environment: production', workflow)
        self.assertIn('cancel-in-progress: false', workflow)
        self.assertIn('group: vps-deploy-${{ inputs.project }}', workflow)
        for name in ('vps_host', 'vps_port', 'vps_ssh_key', 'vps_known_hosts'):
            self.assertIn(f'{name}: ${{{{ secrets.{name} }}}}', workflow)

    def test_download_requires_exactly_one_regular_release_archive(self):
        # Only artifact discovery remains inline; transport tests execute the
        # canonical scripts in test_direct_action.py.
        workflow = WORKFLOW.read_text()
        step = workflow.split('      - name: Locate release archive\n', 1)[1]
        script = textwrap.dedent(step.split('        run: |\n', 1)[1].split('\n      - name:', 1)[0])
        for mode in ('valid', 'nested-with-spaces', 'empty', 'extra', 'wrong-name', 'symlink'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='download fixture ') as directory:
                root = Path(directory)
                artifact = root / 'deploy-artifact'
                artifact.mkdir()
                archive = artifact / 'release.tar.gz'
                if mode == 'nested-with-spaces':
                    archive = artifact / 'nested path' / 'release.tar.gz'
                    archive.parent.mkdir()
                if mode == 'wrong-name':
                    archive = artifact / 'other.tar.gz'
                if mode == 'symlink':
                    target = root / 'target'
                    target.write_bytes(b'archive')
                    archive.symlink_to(target)
                elif mode != 'empty':
                    archive.write_bytes(b'archive')
                if mode == 'extra':
                    (artifact / 'extra').write_bytes(b'extra')
                output = root / 'outputs'
                result = subprocess.run(
                    ['bash', '-c', script],
                    env={**os.environ, 'RUNNER_TEMP': str(root), 'GITHUB_OUTPUT': str(output)},
                    capture_output=True, text=True, timeout=10,
                )
                if mode in ('valid', 'nested-with-spaces'):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_text(), f'path={archive}\n')
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
