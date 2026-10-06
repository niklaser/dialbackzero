# Private retro network

Dial **777** to visit websites and share files through your Dialback Zero.
The server and Pi must be on the same home LAN. This network does not provide
public Internet access.

## 1. Create the keys

Use a Linux server with Docker Engine, Docker Compose, WireGuard kernel support,
Python 3 and `wireguard-tools`. Give it a stable private IPv4 address; your LAN
must not overlap `10.77.0.0/24`. From the repository root, use your server address:

```sh
python3 server/setup.py --endpoint 192.168.1.50:51820 --output ~/dialback-zero-keys
```

This creates `~/dialback-zero-keys/server.env` for the server and
`~/dialback-zero-keys/pi-hub.json` for the Pi. Both contain private keys;
keep them private and backed up. Existing files are never overwritten.
Reuse this bundle when updating the server.

## 2. Start the server

```sh
cd server
docker compose --project-name dialback-zero \
  --env-file "$HOME/dialback-zero-keys/server.env" up -d --build --wait
```

All five services should become healthy. Only WireGuard's UDP port is exposed
on the server's LAN address. If a host firewall is enabled, allow that port
from the Pi. No router port forwarding or public HTTP, DNS or FTP ports are needed.
To update, get the latest source and run the same Compose command again.

## 3. Connect the Pi

Copy `~/dialback-zero-keys/pi-hub.json` securely to the Pi and follow the
[profile import instructions](../docs/setup.md#private-network).
Set the old computer's dial-up connection to obtain its IP address and DNS
automatically, then dial **777**. Dial **2242525** for ordinary Internet access.

| Address | Use |
| --- | --- |
| `http://retro.net/` | Home page and site directory |
| `http://members.retro.net/` | Create an account and edit your website |
| `http://files.retro.net/` | Download shared files |
| `http://172.16.77.1/` | Dialback Zero settings |
| `10.77.0.1` | Private DNS and server tunnel address |

## 4. Make a website

Create an account at `members.retro.net`, choose a domain and edit `index.html`
or upload files. Domains work only inside this network; no purchase is needed.
Each account gets 10 MiB, up to 256 files, with a 2 MiB limit per file.
FTP uses your domain, port **21**, the same account and **passive mode**.

Edit `server/portal/index.html` and redeploy to change the home page.
Back up the private key bundle and the Docker volumes `dialback-zero_sites`
(accounts and websites) and `dialback-zero_downloads` (shared files).
Keep these volumes when updating or stopping the server; `down -v` deletes them.
