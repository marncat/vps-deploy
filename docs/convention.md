# Deployment convention

## Server layout

Each enabled project has a real, deploy-owned root directory. It must not be a
symlink.

```text
/srv/<project>/
├── releases/
│   └── <release-id>/
├── current -> releases/<release-id>
└── shared/                 # optional, never touched by release-deploy
```

Use a setgid project directory so new release files inherit the runtime group:

```sh
sudo install -d -o deploy -g PROJECT_RUNTIME_GROUP -m 2750 /srv/PROJECT
sudo install -d -o deploy -g PROJECT_RUNTIME_GROUP -m 2750 /srv/PROJECT/releases
```

`releases` must remain a pre-created setgid directory. `release-deploy` verifies
this boundary but does not repair its mode: an unprivileged `deploy` process may
not preserve setgid when the directory group is the separate runtime group.

The runtime user only needs read/execute access. Persistent data belongs outside
release directories, normally under `/var/lib/<project>` or an existing
project-specific data directory. Secrets remain in `/etc/<project>`.

## Artifact contract

The application build job must create `release.tar.gz` whose archive root is
the application's complete systemd `WorkingDirectory`. It must not contain a
repository, build cache, persistent data, or server secrets.

Package it before using `actions/upload-artifact`; the inner tar preserves Unix
executable bits:

```sh
staging=$(mktemp -d)
install -m 0755 path/to/server "$staging/server"
cp -a path/to/runtime-assets "$staging/assets"
tar -C "$staging" -czf release.tar.gz .
```

Upload an Actions artifact named `deployable` containing exactly that file.
The common workflow rejects additional files.

## Caller workflow

Build and validate the application in its own repository, then call the common
workflow as a separate job in the same workflow run:

```yaml
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      # Project-specific checkout, tests, build, packaging...
      - uses: actions/upload-artifact@v6
        with:
          name: deployable
          path: release.tar.gz
          if-no-files-found: error

  deploy:
    needs: build
    permissions:
      actions: read
      contents: read
    uses: OWNER/vps-deploy/.github/workflows/deploy.yml@PINNED_COMMIT_SHA
    with:
      project: PROJECT
      artifact_name: deployable
    secrets:
      vps_host: ${{ secrets.VPS_HOST }}
      vps_port: ${{ secrets.VPS_PORT }}
      vps_ssh_key: ${{ secrets.VPS_SSH_KEY }}
      vps_known_hosts: ${{ secrets.VPS_KNOWN_HOSTS }}
```

Pin the reusable workflow to an audited commit SHA. Give organization secrets
access only to repositories that are trusted to deploy all projects with
`artifact_deploy = true`. The common key deliberately creates one shared trust
domain; the forced command still prevents arbitrary shell access.
If the infrastructure repository is private, its Actions access policy must
explicitly allow the caller repositories.

## Health, rollback, and retention

`deployctl restart` waits for every configured unit to become active. When a
loopback `health_url` is configured, it retries HTTP GET until it receives a 2xx
response or the configured deadline expires. Without a URL it waits two seconds
and checks every unit again.

`release-deploy` switches `current` only after safe extraction. A failed restart
or health check switches back and restarts the previous release. When no prior
release exists it stops the service and removes `current`. Only releases carrying
the tool's success marker participate in automatic retention; legacy directories,
`shared`, and project data are untouched.

Exit status `10` means activation failed but recovery completed. Status `12`
means rollback also failed and requires immediate operator inspection. Status
`2` is an input, configuration, archive, or pre-switch failure.

## Operations

Keep the server configuration root-owned and never put secrets in it. Before
enabling artifact deployment for a project, verify its layout and runtime group,
then set `artifact_deploy = true`.

Useful checks:

```sh
sudo deployctl status PROJECT
readlink /srv/PROJECT/current
find /srv/PROJECT/releases -maxdepth 2 -name .vps-deploy-success.json -print
journalctl -u PROJECT.service -n 100 --no-pager
```

Rotate the shared Actions key by adding a second restricted authorized-key line,
updating the GitHub secret, proving one deployment, and only then removing the
old line.
