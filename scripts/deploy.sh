#!/usr/bin/env bash
set -Eeuo pipefail

for required_input in DEPLOY_PROJECT ARCHIVE_PATH ARCHIVE_SHA256 RELEASE_ID VPS_HOST VPS_PORT VPS_SSH_KEY VPS_KNOWN_HOSTS; do
  if [[ -z ${!required_input:-} ]]; then
    echo "::error::$required_input is empty or unavailable."
    exit 1
  fi
done
if [[ ! $DEPLOY_PROJECT =~ ^[a-z0-9][a-z0-9-]{0,62}$ ||
      ! $RELEASE_ID =~ ^[0-9]+-[0-9]+-[0-9a-fA-F]{12}$ ||
      ! $ARCHIVE_SHA256 =~ ^[0-9a-f]{64}$ ]]; then
  echo "::error::Invalid deployment identity."
  exit 1
fi
if [[ ! $VPS_HOST =~ ^[a-zA-Z0-9:][a-zA-Z0-9.:%_-]*$ ]]; then
  echo "::error::VPS_HOST must contain only a hostname or IP."
  exit 1
fi
if [[ ! $VPS_PORT =~ ^[0-9]{1,5}$ ]] || (( 10#$VPS_PORT < 1 || 10#$VPS_PORT > 65535 )); then
  echo "::error::Invalid VPS_PORT."
  exit 1
fi
# Verify that the file has not changed between preparation and transport.
if [[ ${ARCHIVE_PATH##*/} != release.tar.gz || ! -f $ARCHIVE_PATH || -L $ARCHIVE_PATH || ! -s $ARCHIVE_PATH ]]; then
  echo "::error::Archive must be a nonempty regular file named release.tar.gz."
  exit 1
fi
actual_sha256="$(sha256sum < "$ARCHIVE_PATH")"
if [[ ${actual_sha256%% *} != "$ARCHIVE_SHA256" ]]; then
  echo "::error::Archive changed after validation."
  exit 1
fi

umask 077
ssh_dir="$(mktemp -d "${RUNNER_TEMP:?}/vps-deploy.XXXXXXXX")"
trap 'rm -rf -- "$ssh_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%s\n' "$VPS_SSH_KEY" > "$ssh_dir/id_ed25519"
printf '%s\n' "$VPS_KNOWN_HOSTS" > "$ssh_dir/known_hosts"
unset VPS_SSH_KEY VPS_KNOWN_HOSTS
if ! ssh-keygen -y -P '' -f "$ssh_dir/id_ed25519" >/dev/null 2>&1; then
  echo "::error::VPS_SSH_KEY is not a valid unencrypted private key."
  exit 1
fi
if LC_ALL=C grep -q $'\r' "$ssh_dir/known_hosts"; then
  echo "::error::VPS_KNOWN_HOSTS contains CRLF characters."
  exit 1
fi
lookup_host="$VPS_HOST"
if (( 10#$VPS_PORT != 22 )); then
  lookup_host="[$VPS_HOST]:$((10#$VPS_PORT))"
fi
if ! ssh-keygen -F "$lookup_host" -f "$ssh_dir/known_hosts" >/dev/null 2>&1; then
  echo "::error::VPS_KNOWN_HOSTS does not match VPS_HOST/VPS_PORT."
  exit 1
fi

deployment_log="$ssh_dir/deployment.log"
if ssh \
  -i "$ssh_dir/id_ed25519" \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=10 \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$ssh_dir/known_hosts" \
  -p "$((10#$VPS_PORT))" \
  "deploy@$VPS_HOST" \
  "deploy $DEPLOY_PROJECT $RELEASE_ID" < "$ARCHIVE_PATH" | tee "$deployment_log"; then
  :
else
  result=$?
  echo "::error::Deployment was not confirmed (exit $result). Server-side activation or recovery may still be running. Verify service and release before retrying."
  {
    echo "### Deployment not confirmed"
    echo "Project: $DEPLOY_PROJECT; requested release: $RELEASE_ID; exit: $result."
    echo "Server-side activation or recovery may still be running. Verify service and release before retrying; this action does not retry automatically."
  } >> "$GITHUB_STEP_SUMMARY"
  exit "$result"
fi
expected="deployment healthy: $DEPLOY_PROJECT $RELEASE_ID sha256=$ARCHIVE_SHA256"
if ! grep -Fxq -- "$expected" "$deployment_log"; then
  echo "::error::SSH exited successfully but the expected release and archive digest were not confirmed. Verify service and release before retrying."
  {
    echo "### Deployment not confirmed"
    echo "SSH exited successfully, but the completion response did not confirm release $RELEASE_ID and archive SHA-256 $ARCHIVE_SHA256."
    echo "Verify service and release before retrying."
  } >> "$GITHUB_STEP_SUMMARY"
  exit 1
fi
{
  echo "### Deployment confirmed"
  echo "Project: $DEPLOY_PROJECT"
  echo "Release: $RELEASE_ID"
  echo "Archive SHA-256: $ARCHIVE_SHA256"
  echo "Server activation and configured health checks completed successfully."
} >> "$GITHUB_STEP_SUMMARY"
