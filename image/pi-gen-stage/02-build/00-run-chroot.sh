#!/bin/bash -e

SOURCE=/tmp/dialback-zero-build/software/vendor/tcpser

# Raspberry Pi OS armhf normally targets ARMv6, but keep the baseline explicit
# so a compiler-default change cannot silently drop the original Pi Zero.
ARMV6_CFLAGS='-O2 -pipe -march=armv6 -mfpu=vfp -mfloat-abi=hard -fstack-protector-strong -D_FORTIFY_SOURCE=2 -Wall'
ARMV6_LDFLAGS='-Wl,-z,relro,-z,now -lpthread'

make -C "${SOURCE}" clean
make -C "${SOURCE}" \
	CC=gcc \
	CFLAGS="${ARMV6_CFLAGS}" \
	LDFLAGS="${ARMV6_LDFLAGS}"
