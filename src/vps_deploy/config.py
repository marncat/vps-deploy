from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


CONFIG_PATH = Path("/etc/deployctl/projects.toml")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$", re.ASCII)
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*\.service$", re.ASCII)
RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", re.ASCII)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Project:
    name: str
    services: tuple[str, ...]
    artifact_deploy: bool = False
    keep_releases: int = 5
    health_url: str | None = None
    health_timeout_seconds: int = 20
    max_archive_bytes: int = 1_073_741_824
    max_extracted_bytes: int = 4_294_967_296
    max_archive_members: int = 100_000


@dataclass(frozen=True)
class Config:
    projects: dict[str, Project]

    def project(self, name: str) -> Project:
        if not PROJECT_RE.fullmatch(name):
            raise ConfigError(f"invalid project name: {name!r}")
        try:
            return self.projects[name]
        except KeyError as exc:
            raise ConfigError(f"project is not allowed: {name}") from exc


def validate_release_id(value: str) -> str:
    if value in {".", ".."} or not RELEASE_RE.fullmatch(value):
        raise ConfigError(f"invalid release id: {value!r}")
    return value


def _integer(table: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def _health_url(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("health_url must be a string")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ConfigError("health_url must use http and a numeric loopback address")
    if parsed.username or parsed.password or parsed.fragment:
        raise ConfigError("health_url must not contain credentials or a fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("health_url contains an invalid port") from exc
    if port is None or not 1 <= port <= 65535:
        raise ConfigError("health_url must contain an explicit port")
    return value


def parse_config(data: bytes) -> Config:
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    if set(raw) != {"schema_version", "projects"} or raw.get("schema_version") != 1:
        raise ConfigError("config must contain only schema_version = 1 and projects")
    tables = raw.get("projects")
    if not isinstance(tables, dict) or not tables:
        raise ConfigError("projects must be a non-empty TOML table")

    projects: dict[str, Project] = {}
    seen_units: set[str] = set()
    allowed = {
        "services", "artifact_deploy", "keep_releases", "health_url",
        "health_timeout_seconds", "max_archive_bytes", "max_extracted_bytes",
        "max_archive_members",
    }
    for name, table in tables.items():
        if not PROJECT_RE.fullmatch(name) or not isinstance(table, dict):
            raise ConfigError(f"invalid project table: {name!r}")
        unknown = set(table) - allowed
        if unknown:
            raise ConfigError(f"unknown keys for {name}: {', '.join(sorted(unknown))}")
        services = table.get("services")
        if not isinstance(services, list) or not services or not all(isinstance(x, str) for x in services):
            raise ConfigError(f"{name}.services must be a non-empty string array")
        if len(set(services)) != len(services):
            raise ConfigError(f"{name}.services contains duplicates")
        for unit in services:
            if not UNIT_RE.fullmatch(unit):
                raise ConfigError(f"invalid service unit for {name}: {unit!r}")
            if unit in seen_units:
                raise ConfigError(f"service unit is assigned more than once: {unit}")
            seen_units.add(unit)
        artifact_deploy = table.get("artifact_deploy", False)
        if not isinstance(artifact_deploy, bool):
            raise ConfigError(f"{name}.artifact_deploy must be boolean")
        projects[name] = Project(
            name=name,
            services=tuple(services),
            artifact_deploy=artifact_deploy,
            keep_releases=_integer(table, "keep_releases", 5, 1, 100),
            health_url=_health_url(table.get("health_url")),
            health_timeout_seconds=_integer(table, "health_timeout_seconds", 20, 1, 300),
            max_archive_bytes=_integer(table, "max_archive_bytes", 1_073_741_824, 1, 1 << 40),
            max_extracted_bytes=_integer(table, "max_extracted_bytes", 4_294_967_296, 1, 1 << 42),
            max_archive_members=_integer(table, "max_archive_members", 100_000, 1, 1_000_000),
        )
    return Config(projects)


def load_config(path: Path = CONFIG_PATH, *, require_root_owner: bool = True) -> Config:
    if require_root_owner:
        try:
            parent = path.parent.lstat()
        except OSError as exc:
            raise ConfigError(f"cannot inspect config directory {path.parent}: {exc}") from exc
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
            raise ConfigError(f"config directory must be a root-owned, non-writable directory: {path.parent}")
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except OSError as exc:
        raise ConfigError(f"cannot open config {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"config is not a regular file: {path}")
        if require_root_owner and info.st_uid != 0:
            raise ConfigError(f"config must be owned by root: {path}")
        if info.st_mode & 0o022:
            raise ConfigError(f"config must not be group/world writable: {path}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read()
    finally:
        os.close(fd)
    return parse_config(data)
