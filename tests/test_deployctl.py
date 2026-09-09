import unittest
from unittest.mock import patch

from vps_deploy.config import Project
from vps_deploy.deployctl import JOURNALCTL, SYSTEMCTL, _NoRedirect, execute, wait_healthy


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class DeployctlTests(unittest.TestCase):
    def setUp(self):
        self.project = Project("example-suite", ("example-api.service", "example-worker.service"))

    def test_restart_uses_one_systemctl_transaction(self):
        calls = []

        def runner(args):
            calls.append(tuple(args))
            return 0

        with patch("vps_deploy.deployctl.wait_healthy", return_value=True):
            self.assertEqual(execute("restart", self.project, runner), 0)
        self.assertEqual(calls[0], (SYSTEMCTL, "restart", "--", *self.project.services))

    def test_failure_prints_status_and_journal_for_every_unit(self):
        calls = []

        def runner(args):
            calls.append(tuple(args))
            return 1 if len(calls) == 1 else 0

        self.assertEqual(execute("start", self.project, runner), 1)
        for unit in self.project.services:
            self.assertIn((SYSTEMCTL, "status", "--no-pager", "--full", "--", unit), calls)
            self.assertIn((JOURNALCTL, "--no-pager", "--lines", "80", "--unit", unit), calls)

    def test_http_health_requires_active_units_and_2xx(self):
        project = Project(
            "app", ("app.service",), health_url="http://127.0.0.1:1234/health", health_timeout_seconds=1
        )
        self.assertTrue(
            wait_healthy(
                project,
                lambda _args: 0,
                clock=lambda: 0.0,
                sleeper=lambda _seconds: None,
                opener=lambda *_args, **_kwargs: FakeResponse(),
            )
        )
        self.assertFalse(wait_healthy(project, lambda _args: 3, sleeper=lambda _seconds: None))

    def test_health_client_does_not_follow_redirects(self):
        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "http://example.com/"))


if __name__ == "__main__":
    unittest.main()
