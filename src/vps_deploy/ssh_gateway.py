from __future__ import annotations

import os
import subprocess
import sys

from .config import ConfigError, PROJECT_RE, validate_release_id


RELEASE_DEPLOY = "/usr/local/bin/release-deploy"


def parse_original_command(value: str) -> tuple[str, str]:
    parts = value.split(" ")
    if len(parts) != 3 or parts[0] != "deploy" or not PROJECT_RE.fullmatch(parts[1]):
        raise ConfigError("only 'deploy <project> <release-id>' is allowed")
    return parts[1], validate_release_id(parts[2])


def main() -> int:
    os.umask(0o027)
    try:
        project, release_id = parse_original_command(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ConfigError as exc:
        print(f"vps-deploy-ssh: {exc}", file=sys.stderr)
        return 64
    return subprocess.run((RELEASE_DEPLOY, project, release_id), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
