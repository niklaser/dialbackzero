# First boot

## Prepare the card

Follow [the image guide](../image/README.md) to download or build an image.
Write the `.img.xz` with Raspberry Pi Imager 2.x and set your user account,
network and locale. Supported boards: Pi Zero, Zero W/WH and Zero 2 W.

Insert the card, connect Ethernet or configure Wi-Fi on a W model, and power
only the carrier's USB-C input.

## Connect a computer

1. Use a straight-through RS-232 cable: **38400 baud, 8N1, flow control off**.
2. Type `AT` and Enter; expect `OK`. Use `AT$CONFIG` while disconnected for settings.
3. Dial **2242525** with your PPP/dial-up client, using automatic IP and DNS.
4. Open **http://172.16.0.1/** for settings. Restart the device after saving.

The Internet PPP addresses are `172.16.0.1` (Pi) and `172.16.0.2` (computer).
For a TCP BBS, dial its hostname and port, for example `ATDTbbs.example:23`.
If typed characters appear twice, disable local echo in the terminal.

`ATM1` enables dialing sounds; `ATM0` disables them. Set speaker volume in
settings and leave the MUTE jumper open. Complete the
[assembly checks](assembly.md#check-before-closing-the-case) before closing the case.

## Private network

Set up the [server](../server/README.md), then copy its `pi-hub.json` to the Pi's
home directory. Keep this file private: it contains the Pi's WireGuard key.
For an older installation, [update the runtime](../software/README.md#update) first.

On the Pi, import the profile. Disconnect any active call before restarting:

```sh
sudo python3 /usr/local/lib/dialback-zero/hub_setup.py --profile ~/pi-hub.json
sudo systemctl restart dialback-zero.target
```

Use `--replace` only to replace a different existing profile; keep a backup.
Create a second dial-up connection for **777**, using the same serial settings
and automatic IP and DNS. Open **http://retro.net/** for the home page and
**http://files.retro.net/** for downloads.

Private PPP uses `172.16.77.1` (Pi/settings) and `172.16.77.2` (computer).
Open `http://172.16.77.1/` for settings and its HTTP/DNS connection test.
Private DNS is `10.77.0.1`; `http://10.77.0.1/` also opens the server directly.
The home LAN must not overlap `10.77.0.0/24` or either PPP address pair.
Dial **2242525** again for ordinary Internet access.
