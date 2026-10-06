#!/bin/bash -e

SOURCE_DIR="${ROOTFS_DIR}/tmp/dialback-zero-build"
BINARY="${SOURCE_DIR}/software/vendor/tcpser/tcpser"

test -x "${BINARY}"
python3 "${SOURCE_DIR}/software/install.py" \
	--root "${ROOTFS_DIR}" \
	--binary "${BINARY}" \
	--hardware rev-c-ethernet \
	--version "${DIALBACK_IMAGE_VERSION}" \
	--source-commit "${DIALBACK_SOURCE_COMMIT}"

# Export the very same installed release. pi-gen's Docker wrapper copies the
# entire deploy directory to the host, including this precompiled update.
python3 "${SOURCE_DIR}/image/package_update.py" \
	--release-dir "${ROOTFS_DIR}/opt/dialback-zero/current" \
	--output "${DEPLOY_DIR}"
