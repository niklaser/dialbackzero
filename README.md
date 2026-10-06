# Dialback Zero

Dialback Zero lets an old computer get online through its serial port.
It works like a dial-up modem, but connects over Ethernet or Wi-Fi instead of
a phone line.

## Get started

1. [Assemble the board](docs/assembly.md) using the [parts list](docs/self-solder-parts.md).
2. [Print and assemble the enclosure](enclosure/README.md).
3. [Download the Raspberry Pi image](image/README.md) and write it to a microSD card.
4. [Connect your computer and make the first call](docs/setup.md).

Use a straight-through RS-232 cable and **38400 baud, 8N1, flow control off**.
The Rev C serial port supports TX, RX and GND.

| Connection     | Dial number | Settings page         |
|----------------|-------------|-----------------------|
| Internet       | `2242525`   | `http://172.16.0.1/`  |
| Private server | `777`       | `http://172.16.77.1/` |

## Files and guides

- [PCB manufacturing files](manufacturing/rev-c/README.md)
- [Ready-to-print STL files](enclosure/README.md)
- [Software settings](software/README.md)
- [Private server with Docker Compose](server/README.md)
- [GPIO and connector reference](docs/hardware.md)
- [Licenses](LICENSES.md)
