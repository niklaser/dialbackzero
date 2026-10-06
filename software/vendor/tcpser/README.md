This is yet another fork of Jim Brain's tcpser serial to IP modem emulation program.

The original source code can be found here:
http://www.jbrain.com/pub/linux/serial/

My changes are based upon the rc12 archive dated 11Mar09.

I fixed the bug with being unable to connect to real telnet servers.

I also made the modem routines automatically detect parity. If even,
odd, or mark parity is detected then the telnet connection will be
limited to 7 bit. Space parity will be treated as 8N1 and will allow
telnet binary mode.

I also incorporated geneb's changes found at https://github.com/geneb/tcpser

Chris Osborn <fozztexx@fozztexx.com>
http://insentricity.com

## Dialback Zero provenance

This source tree was seeded from `tcpser.tar.gz` in the
[RetroExternalModem project](https://github.com/caiot5/RetroExternalModem). The
archive used while preparing the standalone source tree has SHA-256
`3ae7bd25e9113d082ce1304d23a45bc3dcfce21470f0053b8c4150bab8698c64`.
The installed program does not depend on a RetroExternalModem checkout.

The lineage and credits above are retained from that archive: Jim Brain's
original tcpser, Chris Osborn/FozzTexx's fork, and geneb's changes. The archive
also contains changes associated with Luis Miguel Silva's WiFi2DialUp and the
RetroExternalModem contributors. RetroExternalModem's own documentation names
Jim Brain, FozzTexx, and Luis Miguel Silva and states GPLv3 for that project.

The original `README` in this directory states that tcpser is distributed
under GPL 2.0 or later. Both original README files are deliberately retained
with the source. Dialback Zero's changes add the native event socket,
`AT$CONFIG` helper handoff, installed sound paths, safer phone-book bounds, and
real UART/PTY integration coverage. They also remove the appliance-specific
shell commands that edited Wi-Fi and systemd configuration from AT commands.
