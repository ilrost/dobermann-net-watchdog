# dobermann-net-watchdog

The watchdog of your internet connection. *(Looking for "doberman"? This is it, with two n's.)*

📖 **Guide**: [`docs/GUIDE.md`](docs/GUIDE.md) · 🇮🇹 **Italiano**: [`docs/GUIDA.md`](docs/GUIDA.md)

dobermann watches the path between your home network and the internet: the router, the modem if
there is one, the operator. When the connection breaks it does not just say "it's down": it mails you
**where** it broke, **why**, the **evidence** it collected, **how long** it lasted and **what to do**.

- Works with any router that speaks UPnP (almost all of them), and reads some 4G/5G modems in more
  detail.
- Mail is the only notification channel, with rules per kind of event and a daily or weekly summary.
  Gmail works, as does any provider's SMTP server. A mail written while the internet is down waits in
  dobermann's own queue and goes out when it is back.
- The month's outages as a page to print or save as PDF (and as CSV), to attach to a complaint.
- A speed test, only when you ask for it.
- Web panel over HTTPS: today's incidents, history, charts, settings.
- English, Italiano, Español, Deutsch, Français: in the panel and in the mails.
- No Python packages to install: the standard library only.

## Install

As root, on a Proxmox host (dobermann gets its own LXC container) or on any Linux with systemd:

```bash
git clone https://github.com/ilrost/dobermann-net-watchdog.git /opt/dobermann
cd /opt/dobermann
sudo python3 wizard.py
```

The wizard asks for the language, where to install (on a Proxmox host it offers a new container), the
address, HTTPS or HTTP, and the panel password. It also installs the programs that are missing, after
asking. Everything else is set in the browser: the chain of devices, the mail, when to be told. The
wizard ends with the address to open.

## Requirements

| Needed for | Debian / Ubuntu | Fedora / RHEL | Arch | openSUSE |
|---|---|---|---|---|
| the program (Python 3.11+) | `python3` | `python3` | `python` | `python3` |
| the measurements | `iputils-ping` | `iputils` | `iputils` | `iputils` |
| gateway and addresses | `iproute2` | `iproute` | `iproute2` | `iproute2` |
| checking the mail server | `ca-certificates` | `ca-certificates` | `ca-certificates` | `ca-certificates` |
| the HTTPS certificate | `openssl` | `openssl` | `openssl` | `openssl` |
| the name `dobermann.local` (optional) | `avahi-daemon` `avahi-utils` | `avahi` `avahi-tools` | `avahi` | `avahi` `avahi-utils` |

Tested on Debian 12, in Proxmox containers. Ubuntu and the other families use the same
programs and the wizard knows their package names, but they have not been tried yet. Systems without
systemd are not supported.

## Documentation

- [`docs/GUIDE.md`](docs/GUIDE.md): installation and use, step by step, including setting up Gmail
  ([Italiano](docs/GUIDA.md)).
- [`docs/ADAPTERS.md`](docs/ADAPTERS.md): teaching dobermann to read a new device.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

## License

Copyright (C) 2026 ilrost

dobermann is free software: you can redistribute it and/or modify it under the terms of the GNU
General Public License as published by the Free Software Foundation, either version 3 of the License,
or (at your option) any later version. It is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE. See [`LICENSE`](LICENSE) for the full text.
