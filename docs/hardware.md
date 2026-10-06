# Rev C hardware reference

The PCB is 130 × 80 mm. J1, the Pi socket, mounts underneath; all other parts
mount on top. See [assembly](assembly.md), [hand-soldered parts](self-solder-parts.md)
and [enclosure](../enclosure/README.md).

| Function | BCM GPIO | Physical header pin |
| --- | ---: | ---: |
| UART transmit to RS-232 driver | 14 | 8 |
| UART receive from RS-232 driver | 15 | 10 |
| Mono PWM audio | 12 | 32 |
| Reserved second audio output, not connected | 13 | 33 |
| Power-button interrupt, active low | 3 | 5 |
| Final shutdown power cut, active high | 26 | 37 |
| W5500 MOSI / MISO / clock / chip select | 10 / 9 / 11 / 8 | 19 / 21 / 23 / 24 |
| W5500 interrupt | 4 | 7 |
| MR / NET / SD / RD / CD / OH / AA lights | 23 / 25 / 22 / 27 / 24 / 5 / 6 | 16 / 22 / 15 / 13 / 18 / 29 / 31 |

PWR follows the switched 5 V rail. The other lights are active high: MR means
modem ready, NET means an upstream IPv4 address, SD/RD show data from/to the
computer, CD means connected, OH means off-hook and AA means auto-answer.

DE9 J2 uses pin 2 for RS-232 output, pin 3 for input and pin 5 for ground.
Other signal pins are unconnected; shield mounts are grounded.
Use a **straight-through cable with flow control off**.

The [hardware installer](../software/hardware/install.py) configures UART,
audio, shutdown and Ethernet. LED software must use only the listed LED GPIOs;
power, UART and Ethernet pins belong to kernel drivers.
