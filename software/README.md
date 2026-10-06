# Dialback Zero software

The modem, settings page, PPP and status lights start automatically at boot.
Use the [image guide](../image/README.md) and [first-boot guide](../docs/setup.md)
to get started. The optional [server](../server/README.md) provides the private
network reached by dialing `777`.

## Settings

Open `http://172.16.0.1/` over Internet PPP, `http://172.16.77.1/` over private
PPP, or `http://localhost/` on the Pi. Use `AT$CONFIG` for serial settings while
disconnected. The web page requires no JavaScript.

Settings take effect after a restart.

Speaker volume is 0–100% (default 70%); zero mutes it. `ATM1` enables dialing
sounds and `ATM0` disables them. Leave the board's MUTE jumper open for sound.
Configure Wi-Fi in Raspberry Pi Imager or on the settings page.

## Update

On the settings page, select **Check for updates**, then **Install update**.
Installation disconnects the current call; reconnect afterward and open settings
to see the result. Updates preserve your settings and update Dialback Zero
software only, not Raspberry Pi OS or the microSD image.

## Diagnostics

```sh
systemctl status dialback-zero.target dialback-zero-modem.service
journalctl -u dialback-zero-modem.service -b
sudo wg show wg-dbz latest-handshakes
```

See [LED meanings and GPIOs](../docs/hardware.md) and [licenses](../LICENSES.md).
