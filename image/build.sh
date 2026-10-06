#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly PI_GEN_REPOSITORY='https://github.com/RPi-Distro/pi-gen.git'
readonly PI_GEN_COMMIT="$(tr -d '[:space:]' < "${SCRIPT_DIR}/pi-gen.commit")"
readonly WORK_ROOT="$(python3 -c 'import os, sys; from pathlib import Path; print(Path(os.environ.get("DIALBACK_WORK_DIR", sys.argv[1] + "-work")).expanduser().resolve())' "${PROJECT_DIR}")"
readonly BUILD_DIR="${DIALBACK_BUILD_DIR:-${WORK_ROOT}/build/image}"
readonly PI_GEN_DIR="${BUILD_DIR}/pi-gen"
readonly BUILD_MARKER="${BUILD_DIR}/.dialback-zero-image-work"
readonly OUTPUT_DIR="${DIALBACK_OUTPUT_DIR:-${WORK_ROOT}/releases/image}"
readonly VERSION="${DIALBACK_IMAGE_VERSION:-dev}"
readonly SOURCE_COMMIT="${DIALBACK_SOURCE_COMMIT:-unknown}"

case "${BUILD_DIR}" in
	/*) ;;
	*) echo 'DIALBACK_BUILD_DIR must be an absolute path' >&2; exit 2 ;;
esac

if [ -e "${PI_GEN_DIR}" ] && [ ! -f "${BUILD_MARKER}" ]; then
	echo "Refusing to modify an unmarked build directory: ${BUILD_DIR}" >&2
	exit 2
fi
mkdir -p "${BUILD_DIR}"
touch "${BUILD_MARKER}"
if [ ! -d "${PI_GEN_DIR}/.git" ]; then
	if [ -e "${PI_GEN_DIR}" ]; then
		echo "Refusing to replace non-git path: ${PI_GEN_DIR}" >&2
		exit 2
	fi
	git clone --filter=blob:none --no-checkout "${PI_GEN_REPOSITORY}" "${PI_GEN_DIR}"
fi

case "$(git -C "${PI_GEN_DIR}" remote get-url origin)" in
	https://github.com/RPi-Distro/pi-gen.git|https://github.com/RPI-Distro/pi-gen.git) ;;
	*) echo "Refusing unexpected pi-gen origin in ${PI_GEN_DIR}" >&2; exit 2 ;;
esac

git -C "${PI_GEN_DIR}" fetch --depth 1 origin "${PI_GEN_COMMIT}"
git -C "${PI_GEN_DIR}" checkout --detach --force "${PI_GEN_COMMIT}"
test "$(git -C "${PI_GEN_DIR}" rev-parse HEAD)" = "${PI_GEN_COMMIT}"

# Apply the mirror, build-tool and package-download patches to pinned pi-gen.
# Validate the mirror override and expected source before modifying the tree.
DIALBACK_RASPBIAN_MIRROR="${DIALBACK_RASPBIAN_MIRROR:-https://archive.raspbian.org/raspbian/}" \
	python3 "${SCRIPT_DIR}/patch_pi_gen.py" --pi-gen "${PI_GEN_DIR}"

# These paths are generated and owned by this builder. Clearing them prevents a
# successful old stage or deleted runtime file from leaking into a new image.
rm -rf \
	"${PI_GEN_DIR}/work" \
	"${PI_GEN_DIR}/deploy" \
	"${PI_GEN_DIR}/stage-dialback" \
	"${PI_GEN_DIR}/dialback-source"
cp -a "${SCRIPT_DIR}/pi-gen-stage" "${PI_GEN_DIR}/stage-dialback"
install -d -m 0755 "${PI_GEN_DIR}/dialback-source/software"

# Stage only the files needed by the offline installer. This avoids copying
# developer caches, test binaries, local configuration, or private files.
for directory in assets hardware runtime systemd vendor; do
	rsync -a \
		--exclude '__pycache__/' \
		--exclude '*.py[co]' \
		--exclude '*.o' \
		--exclude '/tcpser/tcpser' \
		--exclude '*.private' \
		--exclude '.env' \
		"${PROJECT_DIR}/software/${directory}/" \
		"${PI_GEN_DIR}/dialback-source/software/${directory}/"
done
install -m 0755 \
	"${PROJECT_DIR}/software/install.py" \
	"${PI_GEN_DIR}/dialback-source/software/install.py"
install -m 0644 \
	"${PROJECT_DIR}/software/release.py" \
	"${PI_GEN_DIR}/dialback-source/software/release.py"
install -m 0755 \
	"${PROJECT_DIR}/software/update_recovery.py" \
	"${PI_GEN_DIR}/dialback-source/software/update_recovery.py"
install -d -m 0755 \
	"${PI_GEN_DIR}/dialback-source/image/pi-gen-stage/00-dependencies"
install -m 0644 \
	"${SCRIPT_DIR}/pi-gen-stage/00-dependencies/00-packages-nr" \
	"${PI_GEN_DIR}/dialback-source/image/pi-gen-stage/00-dependencies/00-packages-nr"
install -m 0755 \
	"${SCRIPT_DIR}/package_update.py" \
	"${PI_GEN_DIR}/dialback-source/image/package_update.py"
cp "${SCRIPT_DIR}/config" "${PI_GEN_DIR}/config"
# pi-gen sources this file inside Docker. Quote these values as data and export
# them so the image installer and update exporter see the same build identity.
python3 - "${PI_GEN_DIR}/config" "${VERSION}" "${SOURCE_COMMIT}" <<'PY'
from pathlib import Path
import shlex
import sys
with Path(sys.argv[1]).open("a", encoding="utf-8") as config:
    config.write("\nexport DIALBACK_IMAGE_VERSION=" + shlex.quote(sys.argv[2]) + "\n")
    config.write("export DIALBACK_SOURCE_COMMIT=" + shlex.quote(sys.argv[3]) + "\n")
PY

# Export only the customized Lite rootfs, never the unmodified stage2 image.
touch "${PI_GEN_DIR}/stage2/SKIP_IMAGES"

chmod +x \
	"${PI_GEN_DIR}/stage-dialback/prerun.sh" \
	"${PI_GEN_DIR}/stage-dialback/01-source/00-run.sh" \
	"${PI_GEN_DIR}/stage-dialback/02-build/00-run-chroot.sh" \
	"${PI_GEN_DIR}/stage-dialback/03-install/00-run.sh" \
	"${PI_GEN_DIR}/stage-dialback/04-verify/00-run.sh"

(
	cd "${PI_GEN_DIR}"
	CONTAINER_NAME="dialback-zero-pigen-$$" PRESERVE_CONTAINER=0 ./build-docker.sh
)

python3 "${SCRIPT_DIR}/package_artifacts.py" \
	--deploy "${PI_GEN_DIR}/deploy" \
	--output "${OUTPUT_DIR}" \
	--version "${VERSION}" \
	--source-commit "${SOURCE_COMMIT}" \
	--pi-gen-commit "${PI_GEN_COMMIT}"
