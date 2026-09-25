#!/bin/bash
# this runs on cron every night at 11:55 pm, copying the files of yesterday to the external drive
# TODO #4 the code should check there is enough space for the transfer. if there is not, it should write to a different spot on the local machine and send me an email
# TODO #5

set -euo pipefail

YESTERDAY=$(date -d 'yesterday' '+%Y%m%d')
DATA_DIR="data/${YESTERDAY}"
ZIP_FILE="data_${YESTERDAY}.zip"
DATA_DEST="/d/${YESTERDAY}"

log() { printf '[%s] %-7s %s\n' "$(date '+%H:%M:%S')" "$1" "${*:2}"; }
info() { log INFO "$@"; }
warn() { log WARNING "$@" >&2; }
die() { log ERROR "$@" >&2; exit 1; }

on_err() {
    local rc=$?
    log ERROR "line ${BASH_LINENO[0]}: '$BASH_COMMAND' exited ${rc}" >&2
    exit "$rc"
}
trap on_err ERR

cd "$(dirname "$0")" || die "cannot cd to script dir"
info "starting to copy files to the external drive"

info "zipping ${DATA_DIR} into ${ZIP_FILE}"
zip -r "${ZIP_FILE}" "$DATA_DIR"
info "zip completed. copying ${ZIP_FILE} into ${DATA_DEST}"
mkdir -p "$DATA_DEST"
rsync -av --remove-source-files "$DATA_DIR" "$DATA_DEST"
