#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[zip-sources] %s\n' "$*"
}

repo_root="$(cd "$(dirname "$0")" && pwd)"
cd "$repo_root"

if ! command -v git >/dev/null 2>&1; then
  log "ERROR: git is required"
  exit 1
fi

if ! command -v zip >/dev/null 2>&1; then
  log "ERROR: zip is required"
  exit 1
fi

out_dir="artifacts/source-archives"
mkdir -p "$out_dir"

old_zips=("$out_dir"/*.zip)
if [[ -e "${old_zips[0]}" ]]; then
  old_count="${#old_zips[@]}"
  log "Removing existing archives: $old_count"
  rm -f -- "$out_dir"/*.zip
fi

ts="$(date +%Y%m%d-%H%M%S)"
out_file="$out_dir/DJI_RC2_KMZsync-source-$ts.zip"

# Zip tracked source files from the working tree so local edits are included.
log "Repository: $repo_root"
log "Output: $out_file"

tracked_count="$(git ls-files | wc -l | tr -d ' ')"
log "Tracked files to archive: $tracked_count"

git ls-files -z | xargs -0 zip -q "$out_file"

if [[ -f "$out_file" ]]; then
  archive_size="$(du -h "$out_file" | awk '{print $1}')"
  log "Created archive successfully ($archive_size)"
  log "$out_file"
else
  log "ERROR: archive was not created"
  exit 1
fi
