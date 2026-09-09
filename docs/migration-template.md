# Existing project migration template

Use this checklist to move an existing systemd application to the shared
artifact deployment protocol. Replace uppercase placeholders locally; do not
commit real hostnames, credentials, secret paths, or production inventory.

## 1. Record and verify the current state

Before changing anything, record the installed unit, runtime identity, current
release target, persistent-data ownership, health endpoint, and rollback
procedure. Back up persistent data and the root-owned unit. Keep the old
deployment credential and sudoers rule until the new path has passed its
rollback drill.

## 2. Prepare the runtime boundary

Create a dedicated nologin system user if the application still runs as the
deployment account. During a maintenance window, stop the service and transfer
only persistent application state to that runtime user.

```sh
sudo useradd --system --user-group --home-dir /var/lib/PROJECT \
  --no-create-home --shell /usr/sbin/nologin PROJECT
sudo install -d -o deploy -g PROJECT -m 2750 /srv/PROJECT
sudo install -d -o deploy -g PROJECT -m 2750 /srv/PROJECT/releases
```

Update the root-owned unit to use `User=PROJECT` and `Group=PROJECT`. Keep its
existing application-specific environment, state-directory, sandboxing, start
command, and shutdown behavior. Its working directory must be
`/srv/PROJECT/current`.

Validate the unit, migrate persistent-data ownership while the service is
stopped, reload systemd, and prove that the existing release still starts under
the new identity before changing the deployment workflow.

## 3. Register the project server-side

Add a root-owned entry to `/etc/deployctl/projects.toml`. Start with artifact
deployment disabled, validate service control and health behavior, then enable
it immediately before the first artifact deployment.

```toml
[projects.PROJECT]
services = ["PROJECT.service"]
artifact_deploy = false
keep_releases = 5
health_url = "http://127.0.0.1:PORT/healthz"
health_timeout_seconds = 30
```

Only numeric loopback health URLs are accepted. A multi-service project lists
every root-approved unit in the order used for service-control transactions.

## 4. Move building into the application repository

The application workflow must install its own toolchain, run tests, assemble a
complete runtime directory, and verify that assembled output before packaging.
Create `release.tar.gz` with the runtime directory contents at the archive root;
include executable permissions, but exclude source-control data, build caches,
persistent data, and secrets.

Upload that tar as the only file in an artifact named `deployable`. A dependent
job then calls the reusable workflow with `actions: read`, `contents: read`, the
server-side project name, explicitly mapped VPS secrets, and a pinned
infrastructure commit SHA. The shared workflow must remain unaware of the
application language and build system.

## 5. Deploy and exercise rollback

Run the first workflow manually. Verify that `current` is a relative symlink
into `releases`, systemd reports the dedicated runtime user, the local health
endpoint succeeds, persistent data is intact, and the runtime user can read and
execute the release without modifying it.

During the same maintenance window, deploy a structurally valid artifact whose
process deliberately fails to start. Exit status `10` means the previous release
was restored successfully. Exit status `12` means rollback also failed and
requires immediate inspection of `systemctl status` and the journal.

## 6. Retire the legacy path

After a normal deployment and rollback drill have both succeeded, remove the old
forced command, project-specific sudoers rule, VPS-to-source-host credential,
and server-side repository checkout. Remove build toolchains from the VPS only
when no other workload needs them. Do not combine this cleanup with unrelated
project or persistent-data migration.
