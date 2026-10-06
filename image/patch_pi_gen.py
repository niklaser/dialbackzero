#!/usr/bin/env python3
"""Apply the reviewed Dialback Zero adjustments to the pinned pi-gen tree."""

from __future__ import annotations

import argparse
import os
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


UPSTREAM_MIRROR = "http://raspbian.raspberrypi.com/raspbian/"
DEFAULT_MIRROR = "https://archive.raspbian.org/raspbian/"
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~%+/-]*$")
PACKAGE_INSTALL_HELPER = r'''install_packages()
{
	local packages="$*"
	on_chroot << EOF
attempt=1
while true; do
	if apt-get -o Acquire::Retries=5 --download-only install -y ${packages}; then
		break
	fi
	if [ "\${attempt}" -ge 3 ]; then
		echo "APT prefetch failed after 3 attempts" >&2
		exit 100
	fi
	sleep \$((attempt * 2))
	attempt=\$((attempt + 1))
done
apt-get --no-download install -y ${packages}
EOF
}

'''


def validated_mirror(value: str) -> str:
    if not value or value != value.strip() or any(character.isspace() for character in value):
        raise ValueError("Raspbian mirror must be one URL without whitespace")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Raspbian mirror URL must use http or https")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Raspbian mirror URL must have a host and no credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Raspbian mirror URL has an invalid port") from exc
    if parsed.query or parsed.fragment or not SAFE_PATH.fullmatch(parsed.path):
        raise ValueError("Raspbian mirror URL must have a simple path and no query or fragment")
    path = parsed.path.rstrip("/") + "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def patch_pi_gen(pi_gen: Path, mirror: str) -> None:
    mirror = validated_mirror(mirror)
    replacements = [
        (pi_gen / "stage0/prerun.sh",
            f'\tbootstrap ${{RELEASE}} "${{ROOTFS_DIR}}" {UPSTREAM_MIRROR}\n',
            f'\tbootstrap ${{RELEASE}} "${{ROOTFS_DIR}}" {shlex.quote(mirror)}\n',
        ),
        (pi_gen / "stage0/00-configure-apt/files/raspbian.sources",
            f"URIs: {UPSTREAM_MIRROR}\n",
            f"URIs: {mirror}\n",
        ),
        (pi_gen / "Dockerfile",
            "        ca-certificates fdisk gpg pigz arch-test \\\n",
            "        ca-certificates fdisk gpg pigz arch-test python3 binutils \\\n",
        ),
        (pi_gen / "stage0/00-configure-apt/00-run.sh",
            'install -m 644 files/raspberrypi-archive-keyring.pgp "${ROOTFS_DIR}/usr/share/keyrings/"\n'
            'on_chroot <<- \\EOF\n',
            'install -m 644 files/raspberrypi-archive-keyring.pgp "${ROOTFS_DIR}/usr/share/keyrings/"\n'
            'install -m 644 /dev/null "${ROOTFS_DIR}/etc/apt/apt.conf.d/80-dialback-retries"\n'
            'printf \'%s\\n\' \'Acquire::Retries "5";\' > '
            '"${ROOTFS_DIR}/etc/apt/apt.conf.d/80-dialback-retries"\n'
            'on_chroot <<- \\EOF\n',
        ),
        (pi_gen / "build.sh",
            "#!/bin/bash -e\n\n# shellcheck disable=SC2119\n",
            "#!/bin/bash -e\n\n" + PACKAGE_INSTALL_HELPER + "# shellcheck disable=SC2119\n",
        ),
        (pi_gen / "build.sh",
            '\t\t\ton_chroot << EOF\n'
            'apt-get -o Acquire::Retries=3 install --no-install-recommends -y $PACKAGES\n'
            'EOF\n',
            '\t\t\tinstall_packages --no-install-recommends $PACKAGES\n',
        ),
        (pi_gen / "build.sh",
            '\t\t\ton_chroot << EOF\n'
            'apt-get -o Acquire::Retries=3 install -y $PACKAGES\n'
            'EOF\n',
            '\t\t\tinstall_packages $PACKAGES\n',
        ),
    ]

    changed = {}
    for path, expected, replacement in replacements:
        text = changed.get(path)
        if text is None:
            text = path.read_text(encoding="utf-8")
        if text.count(expected) != 1:
            raise ValueError(f"pinned pi-gen content changed; refusing to patch {path}")
        changed[path] = text.replace(expected, replacement)

    for path, text in changed.items():
        path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi-gen", required=True, type=Path)
    args = parser.parse_args()
    mirror = os.environ.get("DIALBACK_RASPBIAN_MIRROR", DEFAULT_MIRROR)
    try:
        patch_pi_gen(args.pi_gen, mirror)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"pi-gen patch refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
