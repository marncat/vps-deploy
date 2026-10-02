# vps-deploy

Small shared deployment infrastructure for applications hosted as systemd
services on one VPS. Applications are built in GitHub Actions and uploaded as
immutable tar artifacts; the VPS never clones or builds their repositories.

The trust boundaries are intentionally separate:

- `deployctl` runs through sudo and only controls root-configured systemd units.
- `release-deploy` runs as the unprivileged `deploy` account and only changes a
  whitelisted `/srv/<project>` release tree.
- `vps-deploy-ssh` is an SSH forced-command gateway. The Actions key cannot open
  a shell, use SFTP, or forward ports.

## Server installation

Review [the deployment convention](docs/convention.md) first. On the VPS, run:

```sh
sudo ./install/install.sh
sudo install -o root -g root -m 0644 \
  /etc/deployctl/projects.toml.example /etc/deployctl/projects.toml
sudoedit /etc/deployctl/projects.toml
```

Add the public half of the shared Actions key to `/home/deploy/.ssh/authorized_keys`
as one physical line:

```text
restrict,command="/usr/local/bin/vps-deploy-ssh" ssh-ed25519 REPLACE_WITH_PUBLIC_KEY github-actions-vps-deploy
```

The installer deliberately does not edit `authorized_keys`, system users,
systemd units, active configuration, or old sudoers rules.

## Commands

```sh
sudo deployctl status example-app
sudo deployctl restart example-app
release-deploy example-app manual-001 < release.tar.gz
```

`release-deploy` must not run as root. It requests privileged service changes
only through `sudo -n /usr/local/sbin/deployctl`.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -v
python3 -m compileall -q src tests
bash -n install/install.sh
```

See [the project migration template](docs/migration-template.md) when adopting
the protocol in an existing application.

## Optional deployment storage policy

Administrator configuration can set `max_release_bytes` (aggregate release-tree
bytes), `min_free_bytes` (host reserve), and `prepare_services` (unique allowlisted
oneshot units). Zero byte limits and an empty unit list preserve existing behavior.
`journal_namespace` optionally selects the validated systemd journal namespace
for failure diagnostics.

Under the deployment lock, `release-deploy` atomically writes
`.deploy-request.json` with `release_id` and a `phase` of `receive`, `activate`, or
`cleanup`, then invokes `deployctl prepare`. The unit must treat that file as
untrusted data. Receive and activate failures prevent changing current; cleanup
failures are reported. Upload archives are removed before activation. Interrupted
receiver/staging files and non-current pending releases are reclaimed on retry.
A failed rollback creates `.deployment-needs-recovery`; an administrator must
verify recovery before removing it and retrying. Release filesystem changes stay
unprivileged; only registered service units run privileged policy checks.
