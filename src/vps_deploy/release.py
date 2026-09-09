from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .config import ConfigError, Project, load_config, validate_release_id


PROJECTS_ROOT = Path("/srv")
DEPLOYCTL = "/usr/local/sbin/deployctl"
SUDO = "/usr/bin/sudo"
SUCCESS_MARKER = ".vps-deploy-success.json"
RESERVED_NAMES = {SUCCESS_MARKER, ".vps-deploy-pending.json"}


class ReleaseError(RuntimeError):
    pass


Activator = Callable[[str, str], int]


def activate(action: str, project: str) -> int:
    return subprocess.run(
        (SUDO, "-n", DEPLOYCTL, action, project),
        check=False,
    ).returncode


def _safe_name(raw: str) -> PurePosixPath:
    if "\x00" in raw or raw.startswith("/"):
        raise ReleaseError(f"unsafe archive path: {raw!r}")
    parts = tuple(part for part in PurePosixPath(raw).parts if part not in {"", "."})
    if any(part == ".." for part in parts):
        raise ReleaseError(f"unsafe archive path: {raw!r}")
    return PurePosixPath(*parts)


def _link_stays_inside(member: PurePosixPath, linkname: str, *, hard: bool) -> bool:
    link = PurePosixPath(linkname)
    if link.is_absolute():
        return False
    combined = link if hard else member.parent / link
    depth = 0
    for part in combined.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def _ensure_parent_inside(root: Path, target: Path) -> None:
    resolved_root = root.resolve()
    try:
        target.parent.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise ReleaseError(f"archive entry escapes staging directory: {target}") from exc


def extract_archive(archive: Path, destination: Path, project: Project) -> None:
    try:
        bundle = tarfile.open(archive, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseError(f"invalid gzip tar archive: {exc}") from exc
    with bundle:
        members = []
        for member in bundle:
            members.append(member)
            if len(members) > project.max_archive_members:
                raise ReleaseError("archive contains too many entries")
        if not members:
            raise ReleaseError("archive is empty")
        normalized: dict[PurePosixPath, tarfile.TarInfo] = {}
        expanded = 0
        symlinks: set[PurePosixPath] = set()
        for member in members:
            name = _safe_name(member.name)
            if not name.parts:
                if member.isdir():
                    continue
                raise ReleaseError("archive contains an invalid root entry")
            if name.name in RESERVED_NAMES:
                raise ReleaseError(f"archive uses reserved name: {name.name}")
            if name in normalized:
                raise ReleaseError(f"archive contains duplicate path: {name}")
            if not (member.isdir() or member.isreg() or member.issym() or member.islnk()):
                raise ReleaseError(f"archive contains unsupported file type: {name}")
            if member.issym():
                if not _link_stays_inside(name, member.linkname, hard=False):
                    raise ReleaseError(f"symlink escapes release: {name} -> {member.linkname}")
                symlinks.add(name)
            if member.islnk() and not _link_stays_inside(name, member.linkname, hard=True):
                raise ReleaseError(f"hardlink escapes release: {name} -> {member.linkname}")
            if member.isreg():
                expanded += member.size
                if expanded > project.max_extracted_bytes:
                    raise ReleaseError("archive exceeds expanded size limit")
            normalized[name] = member
        for name in normalized:
            for parent in name.parents:
                if parent in symlinks:
                    raise ReleaseError(f"archive entry is nested below a symlink: {name}")
        for name, member in normalized.items():
            if member.islnk():
                target_name = _safe_name(member.linkname)
                target_member = normalized.get(target_name)
                if target_member is None or not target_member.isreg():
                    raise ReleaseError(f"hardlink target must be a regular archive member: {name}")

        hardlinks: list[tuple[Path, Path]] = []
        for name, member in normalized.items():
            target = destination.joinpath(*name.parts)
            _ensure_parent_inside(destination, target)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.symlink(member.linkname, target)
            elif member.islnk():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                link_name = _safe_name(member.linkname)
                hardlinks.append((target, destination.joinpath(*link_name.parts)))
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = bundle.extractfile(member)
                if source is None:
                    raise ReleaseError(f"cannot read archive member: {name}")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(target, flags, 0o600)
                with source, os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                if member.mode & 0o111:
                    os.chmod(target, 0o550)
        for target, source in hardlinks:
            if not source.is_file() or source.is_symlink():
                raise ReleaseError(f"invalid hardlink source: {source.name}")
            os.link(source, target, follow_symlinks=False)
        for path in sorted(destination.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_symlink():
                continue
            if path.is_dir():
                os.chmod(path, 0o550)
            else:
                current = stat.S_IMODE(path.stat().st_mode)
                os.chmod(path, 0o550 if current & 0o111 else 0o440)


def _read_current(project_root: Path, releases: Path) -> str | None:
    current = project_root / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise ReleaseError(f"current must be a symlink: {current}")
    raw = os.readlink(current)
    target = PurePosixPath(raw)
    if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != "releases":
        raise ReleaseError(f"current has an unsafe target: {raw}")
    release_id = validate_release_id(target.parts[1])
    if not (releases / release_id).is_dir():
        raise ReleaseError(f"current target does not exist: {raw}")
    return release_id


def _swap_current(project_root: Path, release_id: str) -> None:
    temporary = project_root / f".current-new-{os.getpid()}"
    try:
        temporary.unlink(missing_ok=True)
        os.symlink(f"releases/{release_id}", temporary)
        os.replace(temporary, project_root / "current")
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _deployment_signals():
    previous: dict[int, object] = {}

    def interrupted(signum, _frame):
        raise InterruptedError(f"deployment interrupted by signal {signum}")

    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, interrupted)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _acquire_lock(lock_path: Path, timeout: float = 60.0):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o640)
    except OSError as exc:
        raise ReleaseError(f"cannot open deployment lock: {exc}") from exc
    stream = os.fdopen(fd, "r+")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return stream
        except BlockingIOError:
            if time.monotonic() >= deadline:
                stream.close()
                raise ReleaseError("another deployment holds the project lock")
            time.sleep(0.25)


def _write_archive(stream: BinaryIO, output: BinaryIO, maximum: int) -> str:
    digest = hashlib.sha256()
    total = 0
    while chunk := stream.read(1024 * 1024):
        total += len(chunk)
        if total > maximum:
            raise ReleaseError("archive exceeds compressed size limit")
        digest.update(chunk)
        output.write(chunk)
    if total == 0:
        raise ReleaseError("archive input is empty")
    return digest.hexdigest()


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ReleaseError(f"refusing to remove non-directory release path: {path}")
    for current, directories, _files in os.walk(path, topdown=True, followlinks=False):
        os.chmod(current, 0o700)
        for name in directories:
            child = Path(current) / name
            if not child.is_symlink():
                os.chmod(child, 0o700)
    shutil.rmtree(path)


def _cleanup_releases(releases: Path, current_id: str, keep: int) -> None:
    candidates = []
    for path in releases.iterdir():
        if path.is_dir() and not path.is_symlink() and (path / SUCCESS_MARKER).is_file():
            candidates.append(path)
    candidates.sort(key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
    selected = {current_id}
    for path in candidates:
        if path.name == current_id:
            continue
        if len(selected) < keep:
            selected.add(path.name)
            continue
        try:
            _remove_tree(path)
        except (OSError, ReleaseError) as exc:
            print(f"release-deploy: warning: cannot remove {path}: {exc}", file=sys.stderr)


def deploy_release(
    project: Project,
    release_id: str,
    stream: BinaryIO,
    *,
    projects_root: Path = PROJECTS_ROOT,
    activator: Activator = activate,
) -> int:
    validate_release_id(release_id)
    if not project.artifact_deploy:
        raise ReleaseError(f"artifact deployment is disabled for {project.name}")
    project_root = projects_root / project.name
    if project_root.is_symlink() or not project_root.is_dir():
        raise ReleaseError(f"project root must be a real directory: {project_root}")
    root_info = project_root.stat()
    if root_info.st_uid != os.geteuid() or not os.access(project_root, os.W_OK | os.X_OK):
        raise ReleaseError(f"project root must be owned and writable by the current user: {project_root}")
    releases = project_root / "releases"
    if releases.is_symlink():
        raise ReleaseError(f"releases must not be a symlink: {releases}")
    releases.mkdir(mode=0o2750, exist_ok=True)
    if not releases.is_dir() or releases.stat().st_uid != os.geteuid():
        raise ReleaseError(f"releases must be owned by the current user: {releases}")
    os.chmod(releases, 0o2750)

    lock = _acquire_lock(project_root / ".deploy.lock")
    archive: Path | None = None
    staging: Path | None = None
    final = releases / release_id
    switched = False
    old_id: str | None = None
    try:
        if final.exists() or final.is_symlink():
            raise ReleaseError(f"release already exists: {release_id}")
        old_id = _read_current(project_root, releases)
        fd, archive_name = tempfile.mkstemp(prefix=".incoming-", suffix=".tar.gz", dir=releases)
        archive = Path(archive_name)
        with os.fdopen(fd, "wb") as output:
            sha256 = _write_archive(stream, output, project.max_archive_bytes)
        staging = Path(tempfile.mkdtemp(prefix=f".staging-{release_id}-", dir=releases))
        extract_archive(archive, staging, project)
        pending = staging / ".vps-deploy-pending.json"
        pending.write_text(json.dumps({"release_id": release_id, "sha256": sha256}) + "\n", encoding="utf-8")
        os.chmod(pending, 0o440)
        os.chmod(staging, 0o750)
        os.replace(staging, final)
        staging = None
        with _deployment_signals():
            _swap_current(project_root, release_id)
            switched = True
            if activator("restart", project.name) != 0:
                raise ReleaseError(f"activation failed for {release_id}")
            marker = final / SUCCESS_MARKER
            os.replace(final / ".vps-deploy-pending.json", marker)
            os.chmod(final, 0o550)
            os.utime(final, None)
        try:
            _cleanup_releases(releases, release_id, project.keep_releases)
        except OSError as cleanup_exc:
            print(f"release-deploy: warning: retention cleanup failed: {cleanup_exc}", file=sys.stderr)
        print(f"deployment healthy: {project.name} {release_id} sha256={sha256}")
        return 0
    except (ReleaseError, ConfigError, OSError, tarfile.TarError, InterruptedError) as exc:
        print(f"release-deploy: {exc}", file=sys.stderr)
        if switched:
            if old_id is not None:
                print(f"rolling back to {old_id}", file=sys.stderr)
                try:
                    _swap_current(project_root, old_id)
                except OSError as rollback_exc:
                    print(f"rollback symlink failed: {rollback_exc}", file=sys.stderr)
                    return 12
                if activator("restart", project.name) != 0:
                    print("rollback restart also failed; releases were retained", file=sys.stderr)
                    return 12
            else:
                if activator("stop", project.name) != 0:
                    print("initial-deployment recovery stop failed; release was retained", file=sys.stderr)
                    return 12
                current = project_root / "current"
                if current.is_symlink() and os.readlink(current) == f"releases/{release_id}":
                    current.unlink()
            try:
                if final.is_dir() and not final.is_symlink():
                    _remove_tree(final)
            except (OSError, ReleaseError) as cleanup_exc:
                print(f"warning: failed release was retained: {cleanup_exc}", file=sys.stderr)
            return 10
        return 2
    finally:
        if staging is not None and staging.is_dir():
            try:
                _remove_tree(staging)
            except (OSError, ReleaseError):
                pass
        if archive is not None:
            archive.unlink(missing_ok=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="release-deploy")
    result.add_argument("project")
    result.add_argument("release_id")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    if os.geteuid() == 0:
        print("release-deploy: refusing to run as root", file=sys.stderr)
        return 2
    args = parser().parse_args(argv)
    try:
        project = load_config().project(args.project)
        return deploy_release(project, args.release_id, sys.stdin.buffer)
    except (ConfigError, ReleaseError) as exc:
        print(f"release-deploy: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
