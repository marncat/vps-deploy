import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_release as fixtures
from vps_deploy.release import deploy_release
from vps_deploy.deployctl import execute, SYSTEMCTL
from vps_deploy.config import Project, parse_config, ConfigError


class BudgetTests(unittest.TestCase):
    def run_fixture(self, project, activate):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root, project_root = fixtures.ReleaseTests().setup_root(directory.name)
        return root, project_root

    def test_preflight_failure_leaves_old_current_and_never_restarts(self):
        p = Project('app', ('app.service',), artifact_deploy=True, prepare_services=('prepare.service',))
        root, pr = self.run_fixture(p, None)
        old = pr / 'releases' / 'old'
        old.mkdir()
        (pr / 'current').symlink_to('releases/old')
        calls = []
        def activate(action, _):
            request = json.loads((pr / '.deploy-request.json').read_text())
            calls.append((action, request['phase']))
            return 1 if request['phase'] == 'activate' else 0
        rc = deploy_release(p, 'new', io.BytesIO(fixtures.archive_bytes({'file': (b'x', 0o644)})), projects_root=root, activator=activate)
        self.assertEqual(rc, 2)
        self.assertEqual(os.readlink(pr / 'current'), 'releases/old')
        self.assertEqual(calls, [('prepare', 'receive'), ('prepare', 'activate'), ('prepare', 'cleanup')])
        self.assertFalse((pr / 'releases/new').exists())

    def test_total_budget_rejects_before_receiving(self):
        p = Project('app', ('app.service',), artifact_deploy=True, max_release_bytes=1)
        root, pr = self.run_fixture(p, None)
        with patch('vps_deploy.release._write_archive') as writer:
            self.assertEqual(deploy_release(p, 'new', io.BytesIO(b''), projects_root=root), 2)
            writer.assert_not_called()
        self.assertFalse((pr / 'current').exists())

    def test_existing_release_is_never_deleted_on_duplicate_id(self):
        p = Project('app', ('app.service',), artifact_deploy=True)
        root, pr = self.run_fixture(p, None)
        (pr / 'releases/old').mkdir()
        (pr / 'releases/old/precious').write_text('keep')
        self.assertEqual(deploy_release(p, 'old', io.BytesIO(b''), projects_root=root), 2)
        self.assertTrue((pr / 'releases/old/precious').exists())

    def test_receiver_archive_released_before_activation(self):
        p = Project('app', ('app.service',), artifact_deploy=True)
        root, pr = self.run_fixture(p, None)
        def activate(*_):
            self.assertEqual(list((pr / 'releases').glob('.incoming-*')), [])
            return 0
        self.assertEqual(deploy_release(p, 'new', io.BytesIO(fixtures.archive_bytes({'file': (b'x', 0o644)})), projects_root=root, activator=activate), 0)

    def test_prepare_only_starts_administrator_registered_units(self):
        calls = []
        p = Project('app', ('app.service',), prepare_services=('storage.service',))
        self.assertEqual(execute('prepare', p, lambda args: calls.append(args) or 0), 0)
        self.assertEqual(calls, [(SYSTEMCTL, 'start', '--', 'storage.service')])

    def test_config_rejects_invalid_prepare_units_and_limits(self):
        for suffix in ('prepare_services=["../bad"]', 'max_release_bytes=-1', 'min_free_bytes=true', 'prepare_services=["app.service"]'):
            with self.assertRaises(ConfigError):
                parse_config(('schema_version=1\n[projects.app]\nservices=["app.service"]\n' + suffix).encode())

    def test_five_deployments_keep_only_current_and_previous(self):
        p = Project('app', ('app.service',), artifact_deploy=True, keep_releases=2)
        root, pr = self.run_fixture(p, None)
        for i in range(5):
            rc = deploy_release(p, f'release-{i}', io.BytesIO(fixtures.archive_bytes({'file': (bytes([i]), 0o644)})), projects_root=root, activator=lambda *_: 0)
            self.assertEqual(rc, 0)
            self.assertEqual(len(list((pr / 'releases').iterdir())), min(i + 1, 2))
        self.assertEqual(os.readlink(pr / 'current'), 'releases/release-4')
        self.assertEqual({path.name for path in (pr / 'releases').iterdir()}, {'release-3', 'release-4'})

    def test_interrupted_upload_staging_and_pending_are_cleaned(self):
        p = Project('app', ('app.service',), artifact_deploy=True)
        root, pr = self.run_fixture(p, None)
        releases = pr / 'releases'
        (releases / '.incoming-dead.tar.gz').write_bytes(b'partial')
        (releases / '.staging-dead').mkdir()
        orphan = releases / 'dead'
        orphan.mkdir()
        (orphan / '.vps-deploy-pending.json').write_text(json.dumps({'release_id': 'dead'}))
        self.assertEqual(deploy_release(p, 'new', io.BytesIO(fixtures.archive_bytes({'file': (b'x', 0o644)})), projects_root=root, activator=lambda *_: 0), 0)
        self.assertEqual({path.name for path in releases.iterdir()}, {'new'})

    def test_prepare_failure_diagnostics_use_configured_namespace(self):
        calls = []
        p = Project('app', ('app.service',), prepare_services=('storage.service',), journal_namespace='listen-again')
        execute('prepare', p, lambda args: calls.append(args) or 1)
        self.assertIn('--namespace', calls[-1])
        self.assertIn('listen-again', calls[-1])
        self.assertIn('storage.service', calls[-1])
