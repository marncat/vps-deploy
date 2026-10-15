#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ! ${DEPLOY_PROJECT:-} =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
  echo "::error::Invalid deployment project name."
  exit 1
fi
if [[ -z ${ARCHIVE_PATH:-} || ${ARCHIVE_PATH##*/} != release.tar.gz ||
      ! -f $ARCHIVE_PATH || -L $ARCHIVE_PATH || ! -s $ARCHIVE_PATH ||
      $ARCHIVE_PATH == *$'\n'* || $ARCHIVE_PATH == *$'\r'* ]]; then
  echo "::error::Archive must be a nonempty regular file named release.tar.gz."
  exit 1
fi
gzip -t -- "$ARCHIVE_PATH"
if [[ ! ${GITHUB_RUN_ID:-} =~ ^[0-9]+$ ||
      ! ${GITHUB_RUN_ATTEMPT:-} =~ ^[0-9]+$ ||
      ! ${GITHUB_SHA:-} =~ ^[0-9a-fA-F]{40,64}$ ]]; then
  echo "::error::Invalid GitHub run identity."
  exit 1
fi
archive_sha256="$(sha256sum < "$ARCHIVE_PATH")"
archive_sha256="${archive_sha256%% *}"
{
  echo "archive_sha256=$archive_sha256"
  echo "release_id=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${GITHUB_SHA:0:12}"
} >> "$GITHUB_OUTPUT"
