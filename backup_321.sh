#!/usr/bin/env bash
# ==========================================================================
#  backup_321.sh - backup rule 3-2-1 for the local stenographer kit.
#
#  Copy 1: local disk      (DEST_LOCAL)
#  Copy 2: external disk   (DEST_EXTERNAL, a mount point)
#  Copy 3: Tailscale node (DEST_TAILSCALE, a UNC-like path or tailscale target)
#
#  Each copy is verified with sha256sum: the hash of the source file is
#  compared with the hash of the copied file. Copies older than RETENTION_DAYS
#  days are rotated out. The script never asks questions and returns a
#  non-zero exit code on any error.
# ==========================================================================

set -euo pipefail

SRC_DIR="${SRC_DIR:-$HOME/Documents/Stenographer}"
DEST_LOCAL="${DEST_LOCAL:-$HOME/Backups/Stenographer/local}"
DEST_EXTERNAL="${DEST_EXTERNAL:-/mnt/external/Backups/Stenographer/external}"
# UNC-подобный путь через Tailscale, например //ts-node/home/stenographer
DEST_TAILSCALE="${DEST_TAILSCALE:-//ts-node/home/stenographer}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

STAMP="$(date +%Y%m%d-%H%M%S)"

log() { printf '[backup] %s\n' "$*"; }
fail() { log "ERROR: $*"; exit 1; }

log "source    : ${SRC_DIR}"
log "copy 1    : ${DEST_LOCAL}"
log "copy 2    : ${DEST_EXTERNAL}"
log "copy 3    : ${DEST_TAILSCALE}"
log "retention : ${RETENTION_DAYS} days"
log "stamp     : ${STAMP}"

[ -d "${SRC_DIR}" ] || fail "source directory not found: ${SRC_DIR}"

# --------------------------------------------------------------------------
#  make_backup <target root> <label>
#  Copies the source tree, verifies every file with SHA256, rotates old files.
# --------------------------------------------------------------------------
make_backup() {
  local target="$1" label="$2" mismatch=0 count=0

  log "--- ${label} -> ${target}"

  mkdir -p "${target}" || fail "cannot create target: ${target}"

  cp -a "${SRC_DIR}/." "${target}/" || fail "copy failed: ${target}"

  # Сравниваем хэши источника и копии для каждого файла.
  while IFS= read -r -d '' src_file; do
    local rel="${src_file#"${SRC_DIR}/"}"
    local dst_file="${target}/${rel}"
    local src_hash dst_hash
    src_hash="$(sha256sum "${src_file}" | awk '{print $1}')"
    if [ ! -f "${dst_file}" ]; then
      log "MISSING in copy: ${rel}"
      mismatch=1
      continue
    fi
    dst_hash="$(sha256sum "${dst_file}" | awk '{print $1}')"
    count=$((count + 1))
    if [ "${src_hash}" != "${dst_hash}" ]; then
      log "HASH MISMATCH: ${rel}"
      mismatch=1
    fi
  done < <(find "${SRC_DIR}" -type f -print0)

  if [ "${mismatch}" -ne 0 ]; then
    fail "hash verification failed for ${target}"
  fi
  log "verified ${count} file(s) in ${target}"

  # Ротация: файлы старше RETENTION_DAYS дней удаляются.
  find "${target}" -type f -mtime "+${RETENTION_DAYS}" -print -delete >/dev/null || true
  log "rotated files older than ${RETENTION_DAYS} days in ${target}"
}

make_backup "${DEST_LOCAL}" "copy 1 (local)" || exit 3
make_backup "${DEST_EXTERNAL}" "copy 2 (external)" || exit 4
make_backup "${DEST_TAILSCALE}" "copy 3 (tailscale)" || exit 5

log "DONE: three copies created and verified."
