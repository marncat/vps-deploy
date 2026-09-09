#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

if [[ $EUID -ne 0 ]]; then
  echo "install.sh must run as root" >&2
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
readonly REPO_ROOT
readonly LIB_DIR=/usr/local/lib/vps-deploy
readonly CONFIG_DIR=/etc/deployctl
readonly SUDOERS_TARGET=/etc/sudoers.d/deployctl

python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || {
  echo "Python 3.11 or newer is required" >&2
  exit 1
}
for path in /usr/bin/systemctl /usr/bin/journalctl /usr/bin/sudo /usr/sbin/visudo; do
  if [[ ! -x $path ]]; then
    echo "required executable is missing: $path" >&2
    exit 1
  fi
done
if ! id deploy >/dev/null 2>&1; then
  echo "required deployment account does not exist: deploy" >&2
  exit 1
fi

sudoers_candidate=$(mktemp /tmp/deployctl-sudoers.XXXXXX)
cleanup() {
  rm -f -- "$sudoers_candidate"
}
trap cleanup EXIT
install -o root -g root -m 0440 "$REPO_ROOT/sudoers/deployctl" "$sudoers_candidate"
/usr/sbin/visudo -cf "$sudoers_candidate"

install_atomic() {
  local source=$1
  local mode=$2
  local destination=$3
  local temporary="${destination}.new.$$"
  install -o root -g root -m "$mode" "$source" "$temporary"
  mv -f -- "$temporary" "$destination"
}

install -d -o root -g root -m 0755 "$LIB_DIR/vps_deploy" "$CONFIG_DIR"
for source in "$REPO_ROOT"/src/vps_deploy/*.py; do
  install_atomic "$source" 0644 "$LIB_DIR/vps_deploy/$(basename -- "$source")"
done
install_atomic "$REPO_ROOT/bin/deployctl" 0755 /usr/local/sbin/deployctl
install_atomic "$REPO_ROOT/bin/release-deploy" 0755 /usr/local/bin/release-deploy
install_atomic "$REPO_ROOT/bin/vps-deploy-ssh" 0755 /usr/local/bin/vps-deploy-ssh
install_atomic "$REPO_ROOT/config/projects.example.toml" 0644 "$CONFIG_DIR/projects.toml.example"
install_atomic "$sudoers_candidate" 0440 "$SUDOERS_TARGET"
/usr/sbin/visudo -cf "$SUDOERS_TARGET"

echo "Installed deployctl, release-deploy, and vps-deploy-ssh."
if [[ ! -e $CONFIG_DIR/projects.toml ]]; then
  echo "No active config was created. Review projects.toml.example, then install it as $CONFIG_DIR/projects.toml (root:root 0644)."
fi
echo "The installer did not edit authorized_keys, systemd units, users, or legacy sudoers rules."
