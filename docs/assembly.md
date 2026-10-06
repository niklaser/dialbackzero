# Assemble a Rev C board

The factory fits the surface-mount parts, including USB-C J4. Add the
[15 hand-soldered parts](self-solder-parts.md):

1. Check the board against the [Rev C manufacturing package](../manufacturing/rev-c/README.md).
2. Fit the 2 × 20 Pi socket J1 **underneath** the carrier. Fit all other parts
   on top. Follow the LED cathode and C17 polarity markings. LS1 is an 8-ohm
   magnetic speaker, not a piezo sounder.
3. Attach the Pi with its top male header engaging J1 and 12 mm spacers.
   Check full insertion and clearance before applying power.
4. Test outside the enclosure. Supply **5 V through the carrier's USB-C**.
   Do power the Pi through its own power connector.

Leave the MUTE header open for sound. An optional 2.54 mm jumper mutes the speaker.

## Check before closing the case

Follow [first boot](setup.md), then check:

- Switched 5 V and local 3.3 V rails; check for overheating.
- `AT` replies `OK`, with and without an upstream network connection.
- PPP connects, resolves a hostname, transfers data and hangs up.
- A BBS connection transfers data and hangs up.
- PWR, MR, NET, SD, RD, CD, OH and AA lights work.
- `ATM1` enables dialing sounds; `ATM0` silences them.
- Settings work through both the vintage browser and serial terminal.
- A short POWER press shuts Linux down before cutting power; another press
  boots it. A software reboot also works. A long press forces power off.

Fit the [enclosure](../enclosure/README.md), checking Pi, header and connector
clearance before fastening it. Use a short POWER press for normal shutdown.
