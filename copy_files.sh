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
die() { 
    log ERROR "$@" >&2
    local msg="ERROR in $(basename -- "$0"): $*"
    echo "$msg" | mail -s "ERROR at $(date) in $(basename -- "$0")" "landon.thomas1@icloud.com"
    exit 1
}

on_err() {
    local rc=$?
    local err_msg="ERROR line ${BASH_LINENO[0]}: '$BASH_COMMAND' exited ${rc}"
    log "$err_msg" >&2
    echo "$err_msg" | mail -s "ERROR at $(date) in $(basename -- "$0")"
    exit "$rc"
}
trap on_err ERR

cd "$(dirname "$0")" || die "cannot cd to script dir"
info "starting to copy files to the external drive"

info "zipping ${DATA_DIR} into ${ZIP_FILE}"
die "i'm stopping!"
zip -r "${ZIP_FILE}" "$DATA_DIR" || die "failed to zip ${DATA_DIR}"
info "zip completed. copying ${ZIP_FILE} into ${DATA_DEST}"
mkdir -p "$DATA_DEST" || die "failed to make directory ${DATA_DEST}"
rsync -av --remove-source-files "${ZIP_FILE}/" "$DATA_DEST" || die "failed to copy from ${ZIP_FILE} into ${DATA_DEST}"
# checksums on the copy to make sure it is exact. log it

# remove local copies, maybe on a sleep timer???

# email if anything has gone wrong. this will be part of on_err