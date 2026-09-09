import os
import tempfile
import unittest
from pathlib import Path

from vps_deploy.config import ConfigError, load_config, parse_config, validate_release_id


VALID = b"""
schema_version = 1
[projects.example-app]
services = ["example-app.service"]
artifact_deploy = true
health_url = "http://127.0.0.1:8080/healthz"
"""


class ConfigTests(unittest.TestCase):
    def test_valid_config_and_defaults(self):
        project = parse_config(VALID).project("example-app")
        self.assertEqual(project.services, ("example-app.service",))
        self.assertEqual(project.keep_releases, 5)
        self.assertTrue(project.artifact_deploy)

    def test_project_and_release_injection_is_rejected(self):
        config = parse_config(VALID)
        for name in ("ssh", "../example-app", "example-app;id", "-x", "invalid☃"):
            with self.assertRaises(ConfigError, msg=name):
                config.project(name)
        for release in ("../old", "/tmp/x", "a b", ";id", ".", ""):
            with self.assertRaises(ConfigError, msg=release):
                validate_release_id(release)

    def test_duplicate_unit_and_unknown_key_are_rejected(self):
        with self.assertRaisesRegex(ConfigError, "assigned more than once"):
            parse_config(b"""
schema_version = 1
[projects.a]
services = ["same.service"]
[projects.b]
services = ["same.service"]
""")
        with self.assertRaisesRegex(ConfigError, "unknown keys"):
            parse_config(VALID + b"surprise = true\n")

    def test_health_url_is_loopback_http_only(self):
        for url in ("https://127.0.0.1:80/", "http://example.com:80/", "http://127.0.0.1/"):
            with self.assertRaises(ConfigError, msg=url):
                parse_config(VALID.replace(b"http://127.0.0.1:8080/healthz", url.encode()))

    def test_load_rejects_writable_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projects.toml"
            path.write_bytes(VALID)
            os.chmod(path, 0o666)
            with self.assertRaisesRegex(ConfigError, "group/world writable"):
                load_config(path, require_root_owner=False)


if __name__ == "__main__":
    unittest.main()
