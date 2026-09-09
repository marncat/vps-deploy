# Repository guidance

- Keep `deployctl` limited to privileged service control and health checks.
- Keep release filesystem operations in the unprivileged `release-deploy` path.
- Never accept an arbitrary unit, command, path, or shell expression from a caller.
- Use only the Python standard library in server-side tools.
- Preserve the immutable `releases/current` contract and automatic rollback.
- Run `PYTHONPATH=src python3 -m unittest discover -v` after changes.
