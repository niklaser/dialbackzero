#!/bin/bash -e

BINARY="${ROOTFS_DIR}/usr/local/bin/dialback-zero-modem"

test -x "${BINARY}"
file "${BINARY}" | grep -Eq 'ELF 32-bit.*ARM'
readelf -A "${BINARY}" | grep -Eq 'Tag_CPU_arch: v6'

test -f "${ROOTFS_DIR}/etc/dialback-zero/config.json"
test -f "${ROOTFS_DIR}/usr/share/dialback-zero/sounds/dial-up.wav"
test -f "${ROOTFS_DIR}/usr/share/dialback-zero/sounds/busy-signal.wav"
test -d "${ROOTFS_DIR}/usr/local/lib/dialback-zero"

# Component services are owned by one target. Only that target is enabled
# directly. NetworkManager pulls in the Rev C Ethernet preparation unit through
# its drop-in.
TARGET="${ROOTFS_DIR}/etc/systemd/system/dialback-zero.target"
TARGET_WANTS='Wants=dialback-zero-activate.service dialback-zero-leds.service dialback-zero-forwarding.service dialback-zero-network.service dialback-zero-ppp-internet.service dialback-zero-ppp-hub.service dialback-zero-config.service dialback-zero-modem.service'
test -f "${TARGET}"
grep -Fxq "${TARGET_WANTS}" "${TARGET}"

for unit in \
	dialback-zero-activate.service \
	dialback-zero-config.service \
	dialback-zero-forwarding.service \
	dialback-zero-leds.service \
	dialback-zero-modem.service \
	dialback-zero-network.service \
	dialback-zero-ppp-hub.service \
	dialback-zero-ppp-internet.service
do
	test -f "${ROOTFS_DIR}/etc/systemd/system/${unit}"
	test ! -e "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/${unit}"
done

test -f "${ROOTFS_DIR}/etc/systemd/system/dialback-zero-ethernet.service"
test "$(readlink "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/dialback-zero.target")" = '../dialback-zero.target'
test ! -e "${ROOTFS_DIR}/etc/systemd/system/multi-user.target.wants/dialback-zero-ethernet.service"

# Network changes apply only through the target-owned oneshot and never gate
# the modem. The Rev C Ethernet identity unit also remains ordered before
# NetworkManager by its drop-in.
NETWORK_UNIT="${ROOTFS_DIR}/etc/systemd/system/dialback-zero-network.service"
grep -Eq '^After=.*dialback-zero-activate\.service' "${NETWORK_UNIT}"
grep -Eq '^After=.*NetworkManager\.service' "${NETWORK_UNIT}"
grep -Fxq 'PartOf=dialback-zero.target' "${NETWORK_UNIT}"
! grep -Eq '^(Before|RequiredBy)=.*dialback-zero-modem\.service' "${NETWORK_UNIT}"
! grep -Fq 'dialback-zero-network.service' "${ROOTFS_DIR}/etc/systemd/system/dialback-zero-modem.service"

NM_DROPIN="${ROOTFS_DIR}/etc/systemd/system/NetworkManager.service.d/90-dialback-zero-ethernet.conf"
test -f "${NM_DROPIN}"
grep -Fxq 'Wants=dialback-zero-ethernet.service' "${NM_DROPIN}"
grep -Fxq 'After=dialback-zero-ethernet.service' "${NM_DROPIN}"

# pi-gen also repeats these checks during final image export.
test ! -e "${ROOTFS_DIR}/root/.ssh/authorized_keys"
test -z "$(find "${ROOTFS_DIR}/etc/ssh" -maxdepth 1 -type f -name 'ssh_host_*_key' -print -quit)"

rm -rf "${ROOTFS_DIR}/tmp/dialback-zero-build"
