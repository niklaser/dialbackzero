# Raspberry Pi image

Raspberry Pi OS Lite 32-bit for Pi Zero, Zero W/WH and Zero 2 W, with
Dialback Zero preinstalled.

## Download from GitHub

1. Download an image from **Releases**, or open **Actions → Test and build
   Raspberry Pi image and update** and download the artifact from a successful run.
2. Extract the artifact ZIP if needed, then select the `.img.xz` in Raspberry
   Pi Imager using **Use custom**.
3. Set your user account, network and locale, then write the microSD card.
   Enable SSH in Imager if you want remote access.
4. Insert the card into the Pi and power it on. Continue with the
   [setup guide](../docs/setup.md).

After setup, use **Check for updates** and **Install update** on the settings
page. These update Dialback Zero software and preserve your settings; they do
not replace Raspberry Pi OS or require writing the card again.
