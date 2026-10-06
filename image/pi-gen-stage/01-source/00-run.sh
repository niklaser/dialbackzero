#!/bin/bash -e

SOURCE_DIR="${ROOTFS_DIR}/tmp/dialback-zero-build"
rm -rf "${SOURCE_DIR}"
install -d -m 0755 "${SOURCE_DIR}"
rsync -a --delete "${BASE_DIR}/dialback-source/" "${SOURCE_DIR}/"
