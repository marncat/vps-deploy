from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

from .config import ConfigError, Project, load_config


SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
ActionRunner = Callable[[Sequence[str]], int]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_health(request, *, timeout: float):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)


def run_command(args: Sequence[str]) -> int:
    return subprocess.run(list(args), check=False).returncode


def diagnostics(project: Project, runner: ActionRunner) -> None:
    for unit in project.services:
        print(f"--- diagnostics: {unit} ---", file=sys.stderr)
        runner((SYSTEMCTL, "status", "--no-pager", "--full", "--", unit))
        runner((JOURNALCTL, "--no-pager", "--lines", "80", "--unit", unit))


def all_active(project: Project, runner: ActionRunner) -> bool:
    return all(runner((SYSTEMCTL, "is-active", "--quiet", "--", unit)) == 0 for unit in project.services)


def wait_healthy(
    project: Project,
    runner: ActionRunner,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    opener: Callable[..., object] = open_health,
) -> bool:
    if project.health_url is None:
        sleeper(2.0)
        return all_active(project, runner)
    deadline = clock() + project.health_timeout_seconds
    while True:
        if not all_active(project, runner):
            return False
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        try:
            request = urllib.request.Request(project.health_url, headers={"User-Agent": "deployctl/1"})
            with opener(request, timeout=min(2.0, remaining)) as response:
                status = getattr(response, "status", 0)
                if 200 <= status < 300:
                    return True
        except (OSError, urllib.error.URLError, TimeoutError):
            pass
        sleeper(min(1.0, max(0.0, deadline - clock())))


def execute(action: str, project: Project, runner: ActionRunner = run_command) -> int:
    if action == "status":
        result = 0
        for unit in project.services:
            if runner((SYSTEMCTL, "status", "--no-pager", "--full", "--", unit)) != 0:
                result = 1
        return result
    rc = runner((SYSTEMCTL, action, "--", *project.services))
    if rc != 0:
        diagnostics(project, runner)
        return 1
    if action in {"start", "restart"} and not wait_healthy(project, runner):
        print(f"health verification failed for {project.name}", file=sys.stderr)
        diagnostics(project, runner)
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="deployctl")
    result.add_argument("action", choices=("start", "stop", "restart", "status"))
    result.add_argument("project")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    if os.geteuid() != 0:
        print("deployctl: must run as root (normally through sudo)", file=sys.stderr)
        return 2
    args = parser().parse_args(argv)
    try:
        project = load_config().project(args.project)
        return execute(args.action, project)
    except ConfigError as exc:
        print(f"deployctl: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
