# dobermann guide

dobermann watches the road between your network and the internet: the router, the modem if there is
one, the operator. It measures all the time, and when the connection breaks it writes to you **where**
it broke, **why**, with what **evidence**, **how long** it lasted and **what to do**. Every week (or
every day) it sends you a summary.

This guide is for whoever installs and uses it. To teach it to read a new device, see
[`ADAPTERS.md`](ADAPTERS.md) (in Italian). 🇮🇹 The Italian version of this guide is
[`GUIDA.md`](GUIDA.md).

- [1. What you need](#1-what-you-need)
- [2. Installing with the wizard](#2-installing-with-the-wizard)
- [3. Installing on Proxmox (LXC container)](#3-installing-on-proxmox-lxc-container)
- [4. First visit](#4-first-visit)
- [5. The chain to the internet](#5-the-chain-to-the-internet)
- [6. Email notifications](#6-email-notifications)
- [7. Gmail as the sender, step by step](#7-gmail-as-the-sender-step-by-step)
- [8. When to be told, and the summary](#8-when-to-be-told-and-the-summary)
- [9. Reading a report](#9-reading-a-report)
- [10. The panel](#10-the-panel)
- [11. Changing dobermann's address](#11-changing-dobermanns-address)
- [12. Maintenance](#12-maintenance)
- [13. Common problems](#13-common-problems)
- [14. Where your passwords are](#14-where-your-passwords-are)

---

## 1. What you need

- **A machine that is always on, in your home network**: a Proxmox server (recommended: dobermann
  lives in a container of its own) or a Linux machine with **systemd** and Python 3.11 or newer.
- **A free IP address** in your network, outside the range the router hands out by itself (DHCP).
  It is where you will open the panel.
- **A mailbox to send from**: Gmail works (see [chapter 7](#7-gmail-as-the-sender-step-by-step)), as
  does the SMTP server of any other provider.

dobermann watches the connection **from the machine it runs on**: if that machine is off, the
watching stops with it.

### Programs needed

dobermann uses the Python standard library only: **no `pip` packages**. It needs these system
programs; the wizard checks which ones are missing and, once you confirm, installs them.

| Needed for | Debian / Ubuntu | Fedora / RHEL | Arch | openSUSE |
|---|---|---|---|---|
| the program | `python3` | `python3` | `python` | `python3` |
| the measurements | `iputils-ping` | `iputils` | `iputils` | `iputils` |
| gateway and addresses | `iproute2` | `iproute` | `iproute2` | `iproute2` |
| checking the mail server | `ca-certificates` | `ca-certificates` | `ca-certificates` | `ca-certificates` |
| the HTTPS certificate (HTTPS only) | `openssl` | `openssl` | `openssl` | `openssl` |
| the mail queue during outages (optional) | `nullmailer` | `nullmailer` | `nullmailer` | `nullmailer` |
| the name `dobermann.local` (optional) | `avahi-daemon` `avahi-utils` | `avahi` `avahi-tools` | `avahi` | `avahi` `avahi-utils` |

**Supported systems**: Debian 12 (in Proxmox containers) is the tested one. Ubuntu and the other
families in the table use the same programs under the names shown, and the wizard knows them,
but they have not been tried yet. Where `nullmailer` is not available, mail goes out through direct
SMTP, without a queue. A system **without systemd** (Alpine, for example) is not supported.

## 2. Installing with the wizard

As root, on a copy of the repository:

```bash
git clone https://github.com/ilrost/dobermann-net-watchdog.git /opt/dobermann
cd /opt/dobermann
sudo python3 wizard.py
```

Installing has **two phases**:

1. **The wizard, here in the terminal**, asks only what can only be done on the machine:
   - the **language** (English, Italian, Spanish, German, French: it is used by the panel and by the
     mails, and can be changed later from the panel);
   - **where** to install: on a Proxmox host it offers a **new LXC container** (recommended, see
     chapter 3); anywhere else it installs on this machine;
   - the **address** you will reach dobermann at and, if you like, the name **`dobermann.local`** too;
   - **HTTPS** (recommended) or **HTTP** (only if a reverse proxy already sits in front);
   - the **missing programs**, which it installs once you confirm;
   - the **panel password**. You can also press Enter and choose it later in the browser: the wizard
     then prints a **one-time code** that is needed to choose it, so that nobody else on your home
     network can choose it first.

   Every question has an answer ready: Enter accepts it. At the end the wizard shows a summary and
   asks before it changes anything.
2. **The browser**, for everything else: the chain of devices, the mail, when to be told (chapters
   5–8). The wizard ends with the address to open.

What it installs, in short: the system user `dobermann` (the services do not run as root); the
configuration in `/etc/dobermann`, the data in `/var/lib/dobermann`; the services `dobermann` (the
measurements) and `dobermann-web` (the panel); the `dobermann` command for maintenance.

**Without questions**, for a script: every answer can be given as an option, and `--yes` accepts the
rest. `python3 wizard.py --help` lists them. For example:

```bash
sudo python3 wizard.py --yes --language en --where here
```

The password is then chosen in the browser with the one-time code. To give it right away there is
`--password-stdin`, which reads it from standard input: never write it in the command itself, it would
end up in the shell history.

Both phases can also be run with the tools the wizard uses: `tools/install.py` (this machine) and
`tools/proxmox_ct.py` (a container). Without `--apply` they show what they would do and change nothing.

## 3. Installing on Proxmox (LXC container)

On the **Proxmox host**, as root:

```bash
git clone https://github.com/ilrost/dobermann-net-watchdog.git /opt/dobermann-net-watchdog
cd /opt/dobermann-net-watchdog
python3 wizard.py
```

The wizard recognises the Proxmox host and offers **a new LXC container**. It asks for the container
number (it proposes the first free one), the network (bridge), the **address with its mask**, for
example `192.168.1.50/24` (it proposes a free one and checks that nothing answers there already), the
gateway and the storage for the disk. Then it:

- creates an **unprivileged** container from the Debian template already on the host (it downloads
  one only if there is none), 1 core, 256 MB of memory, 4 GB of disk, started with the host;
- installs the programs needed in it, nullmailer and avahi included;
- copies dobermann into `/opt/dobermann` and installs it with the choices made;
- prints the panel address and the **SHA-256 fingerprint** of the HTTPS certificate: write it down, you
  need it on the first visit.

**The same by hand**, without the wizard, with the tool it uses:

```bash
# 1) dry run: shows what it would do, changes nothing
python3 tools/proxmox_ct.py --ip 192.168.1.50/24 --language en

# 2) create the container and install
python3 tools/proxmox_ct.py --ip 192.168.1.50/24 --language en --apply
```

Useful options: `--id 210` (container number), `--gw 192.168.1.1` (gateway, if it is not the host's),
`--bridge vmbr1`, `--storage`, `--disk`, `--http`, `--no-name`, `--ip dhcp` (not recommended: the
panel's address could change). `python3 tools/proxmox_ct.py --help` lists them all. This way the
password is chosen in the browser, with the one-time code printed at the end.

## 4. First visit

1. Open the address printed at the end, for example **`https://192.168.1.50/`** (the `http://` address
   sends you there by itself). **`https://dobermann.local/`** works too on Windows, macOS, iPhone and
   Linux with avahi; on Android not always: use the IP address there.
2. The browser warns that the certificate is **not recognised**: that is normal, it is a certificate
   made by dobermann, not by a public authority. Open the certificate's details and **compare the
   SHA-256 fingerprint** with the one printed during the installation. If they match, accept. It is
   done once per device.
3. If you chose the password in the wizard, log in with it. Otherwise the panel asks for the **setup
   code** printed at the end and for the password to choose (at least 8 characters).
4. The **Settings** page opens: go on with the chain (chapter 5) and the notifications (chapter 6).

## 5. The chain to the internet

The chain is the list of devices between dobermann and the internet, in order: for example
`dobermann → FRITZ!Box → 5G modem → internet`. dobermann uses it to tell **where** the connection
breaks.

- Press **Detect**: dobermann finds the router (the gateway) and, if the router has a private outside
  address, the device above it.
- **A modem in bridge or IP passthrough mode cannot be seen by itself**: write its address in the
  field "Upstream device to add" (for example `192.168.2.1`) and press **Detect** again.
- You can **rename** the devices and **remove** the ones that do not belong; press **Save the chain**.
- The **Reading** column says how dobermann reads each device: *standard* (UPnP, the way almost every
  router offers), *unofficial* (the web interface of one specific model) or *from outside only*
  (dobermann watches it with ping and measurements to the internet).
- The **Password** field appears only for devices that give more data with a password. It is optional;
  some devices accept one login at a time, and dobermann steps back for a few minutes when you log in.

Under the chain dobermann shows the **kind of line**: *radio* (the last device, the one closest to the
internet, is a 4G/5G modem) or *wired* (fibre, DSL, cable). Only with a radio line do the signal chart,
the mobile-network news in the mails and the "Fallback to 3G" category appear.

## 6. Email notifications

In **Settings → Email notifications**:

| Field | What to put |
|---|---|
| Send notifications | tick it to turn them on |
| Recipient | the address where you want the mails |
| Sender | leave it empty: the SMTP user is used (many providers, Gmail included, accept only that) |
| Send through | **Local relay (recommended)**: mails written while the internet is down wait in a queue and go out as soon as it is back. **Direct SMTP**: dobermann talks to the provider by itself, with no queue |
| SMTP: server, port, security, user, password | your mail provider's SMTP details (in its instructions for Outlook/Thunderbird, under "outgoing server") |

Then:

1. press **Send a test mail**: it connects straight to the provider's server, so a wrong detail shows
   at once. If it fails, the panel says why (server not found, wrong port or security, password
   refused…);
2. if the mail arrives (the first time, look in the spam folder too), press **Save**.

The saved password is valid **only for the server and user it was saved with**: if you change either,
the panel asks you to type it again. It is never shown or sent back to the browser.

**Sending "by itself", without a mail provider, does not work in practice**: mail sent from a home
connection (dynamic IP, CGNAT, port 25 often blocked) is refused or lands in the spam folder. An
existing SMTP server to send from is always needed: Gmail is the simplest.

## 7. Gmail as the sender, step by step

Gmail does not accept the password you sign in with for sending from a program: it wants an **app
password**, a **16-letter** password made for that purpose and revocable at any time.

**Advice**: use a **dedicated Gmail account** for sending only, made just for dobermann, and have the
notifications delivered to your everyday address. That way the password saved in dobermann gives no
access to your personal mail.

### 7.1 Turn on 2-Step Verification

App passwords exist only when the account has 2-Step Verification.

1. Sign in with the account that will send the mails.
2. Open **<https://myaccount.google.com/signinoptions/twosv>** and turn it on, if it is not on already.

### 7.2 Create the app password (16 characters)

1. Open **<https://myaccount.google.com/apppasswords>** (still with the sending account).
2. As the name write `dobermann` and press **Create**.
3. Google shows the password **only once**, in four groups of four letters, for example
   `abcd efgh ijkl mnop`. Copy it **without spaces**: `abcdefghijklmnop`.

If the page says the setting is not available: 2-Step Verification is not on, or the account uses
security keys only / "Advanced Protection", or it is a work or school account whose administrator has
turned app passwords off. Google's instructions: <https://support.google.com/accounts/answer/185833>.

### 7.3 Put it into dobermann

In **Settings → Email notifications**:

| Field | Value |
|---|---|
| Send notifications | ✔ |
| Recipient | your everyday address (it need not be Gmail) |
| Sender | empty |
| Send through | Local relay (recommended) |
| SMTP: server | `smtp.gmail.com` |
| SMTP: port | `587` |
| SMTP: security | STARTTLS |
| SMTP: user | the sending Gmail address, in full |
| SMTP: password | the 16 letters, **without spaces** |

Press **Send a test mail**, check that it arrives (the first time look in the spam folder too, and mark
it "not spam"), then **Save**. With the local relay the panel also writes the details into nullmailer's
configuration.

Instead of 587/STARTTLS, port `465` with **SSL/TLS** security works too.

### 7.4 If one day you want to revoke it

At <https://myaccount.google.com/apppasswords> delete the `dobermann` password: from then on dobermann
can no longer send. **Changing the Google account password also revokes every app password**: after a
change, create a new one and put it into the panel.

### 7.5 Typical Gmail errors

| The panel says | What to do |
|---|---|
| "the server closed the connection at login" or "refused user or password" | you are using the account password instead of the app password, or you copied it with spaces or incomplete (it must be 16 letters) |
| "cannot contain spaces" | remove the spaces from the app password |
| "SMTP server or user changed: type the password again" | you changed server or user: the saved password is only valid for the old ones, type it again |
| "the server does not answer on that port" | check port and security: 587 with STARTTLS, or 465 with SSL/TLS |

## 8. When to be told, and the summary

In **Settings → When to be told** choose, for each kind of event, when to get a mail:

| Choice | Meaning |
|---|---|
| **At once** | a mail as soon as the event starts |
| **If it lasts more than…** | a mail only if the event is still going on after those minutes; if it ends sooner, it shows up in the summary |
| **Summary only** | no mail, just a line in the summary |
| **Never** | it stays in the panel only |

The initial settings: **Internet down** if it lasts more than 2 minutes; **DNS not answering** if it
lasts more than 5 minutes; **IP renewal / change**, **Slow or unstable network** and **Fallback to 3G**
(radio line only) in the summary only. An IP renewal by the operator usually lasts a few seconds: with
these settings it does not write to you. When the "it is down" mail goes out, the "it is back" one with
the full report follows when the fault is over.

**Summary**: off, every day or every week, on the day and at the hour you choose (initially: Monday at
8). It holds the connection's availability, the events by kind compared with the period before, the
longest one, the IP changes and, with a radio line, the time on each technology (5G/4G/3G), the drops
to worse technologies, the typical and the worst signal. With the tick "even if nothing happened" it
also arrives in quiet weeks: it confirms that dobermann is alive and that mails arrive.
**Send a summary of the last 7 days now** shows you straight away what it looks like.

## 9. Reading a report

```
WHERE
  dobermann ✔ → FRITZ!Box ✔ → modem ✔ → operator ✘ → internet ?
  The fault was between the modem and the operator: everything at home was working.
WHY        the cause in plain words, and the sequence if there was more than one
EVIDENCE   what dobermann measured or the devices reported
HOW LONG   start, end, duration, and how many times it already happened in the last 7 and 30 days
WHAT TO DO
```

Marks: **✔** working, **✘** broken there, **⚠** working but degraded, **?** cannot be checked (beyond the
broken point). dobermann writes only what it measured: when a piece of data is missing, the sentence is
missing, it is never guessed. The mail sent while the fault is still going on has the same structure
with the data of the latest sample; if the internet is down it goes out (from the queue) when the line
is back.

## 10. The panel

At the top right of every page there are four icons:

- **language** (the globe): English, Italian, Spanish, German, French. The choice is used by the panel
  and by the mails;
- **settings** (the gear);
- **user**: since when you are connected, *Change password*, *Log out of every device* (useful if you
  opened the panel on a computer that is not yours);
- **log out**.

The pages:

- **Today**: the day's incidents, bandwidth, latency, loss and (with a radio line) signal charts, IP
  changes. It refreshes by itself; the slider at the top changes the measurement interval.
- **History**: day, week, month, year; click an incident to see it on the chart.
- **Export**: the samples or the hourly summaries as CSV.
- **Settings**: chain, notifications, when to be told, measurements (reference servers, DNS,
  thresholds, days the samples are kept: incidents and hourly summaries are kept for good),
  dobermann's address, panel password.

Changes apply from the next sample, without restarting anything.

## 11. Changing dobermann's address

In **Settings → dobermann's address** you see where the panel is reached and the link that goes into
the mails (you can correct it and save it).

To **change the IP address** write the new one with its mask (for example `192.168.1.60/24`) and the
gateway, then press **Prepare the address change**. dobermann:

1. remakes the HTTPS certificate, valid for both the old and the new address, and uses it at once;
2. updates the link in the mails;
3. shows you **the exact command** to run to change the machine's address.

The panel does **not** change the machine's network by itself: it has no permission to do so, on
purpose. The command depends on where dobermann runs:

| Where | What it shows you |
|---|---|
| Proxmox container | a `pct set <number> -net0 …` to run **on the host**, as root, which keeps the container's MAC, then `pct reboot <number>`. Inside the container the address cannot be changed: the host decides it, and would write it back at the next start |
| NetworkManager | the `nmcli` commands to run on this machine |
| ifupdown (`/etc/network/interfaces`) or systemd-networkd | the lines to write in the file and the command that applies them |

After the change, open the panel at the new address. The name `dobermann.local` follows by itself.

## 12. Maintenance

**Updating dobermann** (on the Proxmox host, in the updated copy of the repository):

```bash
cd /opt/dobermann-net-watchdog && git pull
python3 tools/proxmox_ct.py --update <CT number> --apply
```

It replaces the code (the previous one stays in `/opt/dobermann.old`), installs any new packages and
restarts the services. Configuration, passwords, data and the installation's choices (HTTP or HTTPS,
local name) are not touched. On another Linux: `git pull`, then `sudo python3 tools/install.py --apply`.

**Inside the container** (`pct enter <CT number>`):

```bash
systemctl status dobermann dobermann-web     # state of the services
journalctl -u dobermann -n 50                # last lines of the log
dobermann mail-test                          # a test mail with the saved settings
dobermann probe 192.168.1.1                  # what dobermann can read from a device
```

**Forgotten panel password**: inside the container (or on the machine) run `dobermann reset-password`
and choose a new one; or `dobermann reset-password --code` to remove it and choose it in the browser
with the code it prints. The other settings stay.

**Switching between HTTPS and HTTP**: run the installation again with `--http` or `--https`
(`sudo python3 tools/install.py --apply --http`; for a container, from the host:
`pct exec <number> -- python3 /opt/dobermann/tools/install.py --apply --http`).

**Backup**: the container backups made by Proxmox include configuration, passwords and history.

## 13. Common problems

| Problem | What to do |
|---|---|
| The browser warns about the certificate | normal on the first visit: compare the fingerprint and accept (chapter 4). After an address change the certificate is new: the fingerprint is the one shown in Settings |
| "Too many wrong attempts: try again in a minute" | after 5 wrong passwords in a minute the panel pauses for a minute |
| The test mail arrives but the alerts do not | check "Send notifications" and the rules in **When to be told**: many events go only into the summary |
| Mails arrive late | if the internet was down, they go out from the queue when the line is back: that is intended |
| "Detect" finds only the router | a modem in bridge or IP passthrough mode cannot be seen by itself: add its address |
| A device is "from outside only" | dobermann cannot read it (for example UPnP is off on the router): it watches it anyway with ping and measurements to the internet |
| The panel has no signal chart | it appears only with a radio line and if the modem provides the data |
| `dobermann.local` does not open | on Android it often does not work: use the IP address. Elsewhere check that the network does not block mDNS (many guest networks do) |
| "Wrong setup code" | the code is the one printed at the end of the installation; if you lost it, run `dobermann reset-password --code` on the machine |
| The `dobermann` command is not found inside `pct exec` | use the full path `/usr/local/bin/dobermann`, or get in with `pct enter` |

## 14. Where your passwords are

- **Panel password**: in `/etc/dobermann/admin.json`, not in clear text but as a hash (PBKDF2). The
  wizard takes it only from the keyboard (or from standard input) and writes it nowhere else.
- **SMTP and device passwords**: in `/etc/dobermann/secrets.json`, readable only by the `dobermann`
  user. With the local relay, the SMTP password is also in `/etc/nullmailer/remotes` (readable only by
  `dobermann` and nullmailer), because nullmailer must be able to use it.
- Passwords are **never** sent back to the browser, written to the logs or put in the mails.
- They have to be kept in clear text to be used: whoever administers the container or the Proxmox host
  can read them, and they are in the container's backups. That is why a dedicated, revocable app
  password is a good idea (chapter 7).
