import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from vps_deploy.config import Project
from vps_deploy.release import (
    ReleaseError,
    SUCCESS_MARKER,
    _acquire_lock,
    deploy_release,
    extract_archive,
)


def archive_bytes(files=None, *, symlink=None, special=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for name, (contents, mode) in (files or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            info.mode = mode
            bundle.addfile(info, io.BytesIO(contents))
        if symlink:
            name, target = symlink
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            bundle.addfile(info)
        if special:
            info = tarfile.TarInfo(special)
            info.type = tarfile.FIFOTYPE
            bundle.addfile(info)
    return output.getvalue()


class ReleaseTests(unittest.TestCase):
    def project(self, keep=5):
        return Project("app", ("app.service",), artifact_deploy=True, keep_releases=keep)

    def setup_root(self, directory):
        root = Path(directory)
        project_root = root / "app"
        project_root.mkdir(mode=0o750)
        return root, project_root

    def test_success_preserves_executable_and_switches_relative_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root, project_root = self.setup_root(directory)
            calls = []
            payload = archive_bytes({"app": (b"binary", 0o755), "web/index.html": (b"ok", 0o644)})
            rc = deploy_release(self.project(), "1-1-aabbccddeeff", io.BytesIO(payload), projects_root=root,
                                activator=lambda action, project: calls.append((action, project)) or 0)
            self.assertEqual(rc, 0)
            self.assertEqual(os.readlink(project_root / "current"), "releases/1-1-aabbccddeeff")
            release = project_root / "releases/1-1-aabbccddeeff"
            self.assertTrue((release / "app").stat().st_mode & 0o100)
            self.assertEqual(release.stat().st_mode & 0o777, 0o550)
            self.assertTrue((release / SUCCESS_MARKER).is_file())
            self.assertEqual(calls, [("restart", "app")])

    def test_failed_activation_rolls_back_and_removes_failed_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root, project_root = self.setup_root(directory)
            releases = project_root / "releases"
            old = releases / "old"
            old.mkdir(parents=True)
            (project_root / "current").symlink_to("releases/old")
            results = iter((1, 0))
            calls = []

            def activator(action, project):
                calls.append((action, project))
                return next(results)

            rc = deploy_release(self.project(), "new", io.BytesIO(archive_bytes({
                                    "app": (b"x", 0o755), "web/index.html": (b"x", 0o644)
                                })),
                                projects_root=root, activator=activator)
            self.assertEqual(rc, 10)
            self.assertEqual(os.readlink(project_root / "current"), "releases/old")
            self.assertFalse((releases / "new").exists())
            self.assertEqual(calls, [("restart", "app"), ("restart", "app")])

    def test_first_activation_failure_stops_and_removes_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root, project_root = self.setup_root(directory)
            calls = []

            def activator(action, _project):
                calls.append(action)
                return 1 if action == "restart" else 0

            rc = deploy_release(self.project(), "new", io.BytesIO(archive_bytes({"app": (b"x", 0o755)})),
                                projects_root=root, activator=activator)
            self.assertEqual(rc, 10)
            self.assertFalse((project_root / "current").exists())
            self.assertEqual(calls, ["restart", "stop"])

    def test_rollback_failure_returns_emergency_status_and_retains_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root, project_root = self.setup_root(directory)
            old = project_root / "releases/old"
            old.mkdir(parents=True)
            (project_root / "current").symlink_to("releases/old")
            rc = deploy_release(self.project(), "new", io.BytesIO(archive_bytes({"app": (b"x", 0o755)})),
                                projects_root=root, activator=lambda *_args: 1)
            self.assertEqual(rc, 12)
            self.assertEqual(os.readlink(project_root / "current"), "releases/old")
            self.assertTrue((project_root / "releases/new").exists())

    def test_retention_only_removes_managed_successful_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            root, project_root = self.setup_root(directory)
            releases = project_root / "releases"
            releases.mkdir()
            for index in range(3):
                release = releases / f"old-{index}"
                release.mkdir()
                (release / SUCCESS_MARKER).write_text("{}")
                os.utime(release, (index + 1, index + 1))
            legacy = releases / "legacy"
            legacy.mkdir()
            rc = deploy_release(self.project(keep=2), "new", io.BytesIO(archive_bytes({"app": (b"x", 0o755)})),
                                projects_root=root, activator=lambda *_args: 0)
            self.assertEqual(rc, 0)
            self.assertTrue(legacy.exists())
            managed = [p for p in releases.iterdir() if p.is_dir() and (p / SUCCESS_MARKER).exists()]
            self.assertEqual(len(managed), 2)

    def test_retention_keeps_current_even_when_it_is_oldest(self):
        with tempfile.TemporaryDirectory() as directory:
            root, project_root = self.setup_root(directory)
            releases = project_root / "releases"
            releases.mkdir()
            old_current = releases / "current-old"
            old_current.mkdir()
            (old_current / SUCCESS_MARKER).write_text("{}")
            (project_root / "current").symlink_to("releases/current-old")
            for index in range(2):
                release = releases / f"newer-{index}"
                release.mkdir()
                (release / SUCCESS_MARKER).write_text("{}")
                os.utime(release, (index + 10, index + 10))
            rc = deploy_release(self.project(keep=2), "new", io.BytesIO(archive_bytes({"app": (b"x", 0o755)})),
                                projects_root=root, activator=lambda *_args: 0)
            self.assertEqual(rc, 0)
            managed = [p for p in releases.iterdir() if p.is_dir() and (p / SUCCESS_MARKER).exists()]
            self.assertEqual(len(managed), 2)
            self.assertTrue((releases / "new").exists())

    def test_unsafe_archives_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            destination.mkdir()
            cases = (
                archive_bytes({"../escape": (b"x", 0o644)}),
                archive_bytes(symlink=("link", "../../escape")),
                archive_bytes(special="pipe"),
            )
            for index, payload in enumerate(cases):
                path = Path(directory) / f"bad-{index}.tar.gz"
                path.write_bytes(payload)
                with self.assertRaises(ReleaseError, msg=index):
                    extract_archive(path, destination, self.project())

    def test_duplicate_and_oversized_archive_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            destination.mkdir()
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w:gz") as bundle:
                for contents in (b"first", b"second"):
                    info = tarfile.TarInfo("same")
                    info.size = len(contents)
                    bundle.addfile(info, io.BytesIO(contents))
            duplicate = Path(directory) / "duplicate.tar.gz"
            duplicate.write_bytes(output.getvalue())
            with self.assertRaisesRegex(ReleaseError, "duplicate"):
                extract_archive(duplicate, destination, self.project())

            oversized = Path(directory) / "oversized.tar.gz"
            oversized.write_bytes(archive_bytes({"file": (b"too big", 0o644)}))
            limited = Project("app", ("app.service",), artifact_deploy=True, max_extracted_bytes=2)
            with self.assertRaisesRegex(ReleaseError, "expanded size"):
                extract_archive(oversized, destination, limited)

    def test_compressed_size_limit_fails_before_switch_and_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root, project_root = self.setup_root(directory)
            limited = Project("app", ("app.service",), artifact_deploy=True, max_archive_bytes=2)
            rc = deploy_release(limited, "new", io.BytesIO(b"more than two bytes"), projects_root=root,
                                activator=lambda *_args: self.fail("activation must not run"))
            self.assertEqual(rc, 2)
            self.assertFalse((project_root / "current").exists())
            self.assertEqual(list((project_root / "releases").iterdir()), [])

    def test_lock_rejects_concurrent_holder(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "lock"
            first = _acquire_lock(lock_path, timeout=0)
            try:
                with self.assertRaisesRegex(ReleaseError, "another deployment"):
                    _acquire_lock(lock_path, timeout=0)
            finally:
                first.close()


if __name__ == "__main__":
    unittest.main()
