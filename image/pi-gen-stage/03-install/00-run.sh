#!/bin/bash -e

SOURCE_DIR="${ROOTFS_DIR}/tmp/dialback-zero-build"
BINARY="${SOURCE_DIR}/software/vendor/tcpser/tcpser"

test -x "${BINARY}"
python3 "${SOURCE_DIR}/software/install.py" \
	--root "${ROOTFS_DIR}" \
	--binary "${BINARY}" \
	--hardware rev-c-ethernet
