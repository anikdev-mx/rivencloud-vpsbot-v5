# RIVEN CLOUD VPS Manager Bot (v5)

A Discord bot that deploys and manages Docker-based VPS containers, with full
`systemctl` support, **direct root SSH access** (real IP, port, username, and
root password — works with Termius, PuTTY, or plain `ssh`), a **redeem code**
system, and a **multi-node** system so you can host VPS across more than one
physical server, all from one bot.

---

## ✨ Features

- 🐉 1-click VPS deploy from Discord (`/deploy` or `/create`)
- 🖥 Real Ubuntu/Debian containers with full `systemd` support
- 🔑 Direct root SSH — real IPv4 + NAT port + root password sent to the user's DM
- 🎟️ **Redeem codes** — generate one-time codes that let members claim their own VPS
- 📡 **Multi-node support** — connect other physical servers as "nodes" and deploy VPS on any of them
- 📊 Live status: `RIVEN CLOUD | {n} VPS Running`
- 🔄 Start / stop / restart / reinstall / regen-ssh commands
- ⏰ Optional auto-expiry / auto-suspend
- 🛡 Admin-only management commands
- 🐧 Optional Pterodactyl panel integration

---

## 📋 Requirements

- A Linux server (Ubuntu 22.04/24.04 recommended) with a **public IP address** — this is your **main bot server**
- **Docker** installed and running on it
- **Python 3.10+**
- A **Discord Bot Token**
- (Optional) One or more **extra servers** if you want to run VPS across multiple machines ("nodes")

---

## 🚀 Setup — Main Bot — Step by Step

### 1. Clone the repository

```bash
git clone https://github.com/anikdev-mx/rivencloud-vps-bot-v5.git
cd rivencloud-vps-bot-v5
```

### 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable docker
sudo systemctl start docker
docker ps   # should list (empty) with no errors
```

### 3. Install Python & dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Create your Discord Bot

1. https://discord.com/developers/applications → **New Application**
2. **Bot** → **Add Bot** → enable **Server Members Intent** + **Message Content Intent**
3. **Reset Token** → copy it
4. **OAuth2 → URL Generator** → scopes `bot`, `applications.commands`; permissions `Send Messages`, `Embed Links`, `Use Slash Commands`
5. Open the generated URL and invite the bot to your server

### 5. Configure `.env`

```bash
cp env.example.txt .env
nano .env
```

Fill in at minimum:

```env
DISCORD_TOKEN=your_bot_token_here
ADMIN_ROLE_ID=your_admin_role_id
ADMIN_USER_IDS=your_discord_user_id
SERVER_IP=your_server_public_ip
```

Find your public IP with: `curl -4 ifconfig.me`

### 6. Open firewall ports

```bash
sudo ufw allow 20000:29999/tcp   # SSH range for deployed VPS
sudo ufw allow 8788/tcp          # AGENT_PORT — for remote nodes to connect
```

Also open both ranges in your cloud provider's Security Group / Firewall panel
if you're on AWS, GCP, Azure, Contabo, Hetzner, etc.

### 7. Run the bot 
```bash
apt install python3-pip -y
python3 rivencloud.py
```

You should see:

```
Database ready.
Node-agent WebSocket server listening on 0.0.0.0:8788
Starting RIVEN CLOUD VPS Manager...
```

### 8. Keep it running 24/7

Create ```nano /etc/systemd/system/rivencloud.service```

```ini
[Unit]
Description=RIVEN CLOUD VPS Manager Bot
After=docker.service network.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/root/rivencloud-vpsbot
ExecStart=/root/rivencloud-vpsbot/venv/bin/python3 rivencloud.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable rivencloud
sudo systemctl start rivencloud
sudo systemctl status rivencloud
journalctl -u rivencloud -f     # live logs
```

---

## 💬 Commands

### User commands
| Command | Description |
|---|---|
| `/create` (via `/deploy` wizard) | — |
| `/start <id>` | Start a stopped VPS |
| `/stop <id>` | Stop a running VPS |
| `/restart <id>` | Restart a VPS |
| `/reinstall <id>` | Wipe and rebuild a VPS (new root password) |
| `/regen-ssh <id>` | Get a fresh tmate backup SSH link |
| `/vps-performance <id>` | Live CPU/RAM/disk stats |
| `/my-vps` | List all your VPS instances |
| `/redeem <code>` | Claim a VPS using a redeem code |
| `/commands` | Show all available commands |

### Admin commands
| Command | Description |
|---|---|
| `/deploy` | 1-click VPS deploy wizard (OS → CPU → **Node** → specs) |
| `/create` | Full-parameter VPS creation, with a `node` option |
| `/admin-add-user` / `/admin-remove-user` | Grant/revoke hosting access |
| `/extend-vps`, `/suspend-vps`, `/unsuspend-vps`, `/remove-vps`, `/fix-vps` | VPS lifecycle management |
| `/list-vps` | List every VPS on the node |
| `/node-stats` | Host server resource usage |
| `/check-network` | Diagnose `SERVER_IP` + SSH port setup |
| `/gen-redeem <ram> <cpu> <disk> <valid_days> <count>` | Generate one-time redeem code(s) |
| `/redeem-stock` | View all unredeemed codes |
| `/node-create <name>` | Register a new node |
| `/node-config <name>` | Get the install + connect command for a node |
| `/node-list` | List all nodes and their online/offline status |
| `/node-delete <name>` | Delete a node |
| `/ptero-status` | Pterodactyl panel connection status |

---

## 🎟️ Redeem Code System

1. Admin runs `/gen-redeem ram:2048 cpu:2 disk:20 valid_days:30 count:5` — generates 5 codes,
   each good for a 2GB/2-core/20GB VPS that auto-suspends after 30 days.
2. Admin gives codes out (giveaway, shop, whatever you like).
3. A member runs `/redeem SN-XXXX-XXXX-XXXX` — the bot **atomically** claims and
   deletes the code (so it can never be redeemed twice, even if two people try
   at the exact same time), then deploys the VPS straight to their DMs.
4. `/redeem-stock` lets admins see what codes are still unclaimed.

---

## 📡 Multi-Node System

This lets you plug in **other physical servers** so VPS can be created on them
too, all managed from the one Discord bot. Each node runs a small agent script
that connects **outbound** to your main bot — the node itself doesn't need any
inbound ports open, only your main bot server does (the `AGENT_PORT` you opened
in Step 6).

### Adding a node — Admin side

1. **`/node-create name:Node1`** — registers the node (starts offline) and gives you a secure token internally.
2. **`/node-config name:Node1`** — the bot replies with:
   - An **install command** to run on the new server (downloads `node_agent.py`)
   - A **connect string** — a short `NodeName|token|ip|port` code — to paste into the agent's menu

### Adding a node — On the new server

Log into the **new** server (not your main bot server) and run the install command
the bot gave you:

```bash
curl -o node_agent.py https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/YOUR_REPOSITORY/main/node_agent.py \
  && sudo python3 node_agent.py
```

This shows a menu:

```
1. Install VPS Bot
2. Uninstall VPS Bot
3. Connect NODE
4. Exit
```

**What each option does:**

| Option | What happens |
|---|---|
| **1. Install VPS Bot** | Installs Docker + the Python packages the agent needs on *this* machine. Run this **first**, once. |
| **2. Uninstall VPS Bot** | Removes Docker and every container it created on this machine. Asks for confirmation first — this is destructive. |
| **3. Connect NODE** | Asks you to paste the connect string from `/node-config`. Once pasted, this machine opens a live connection to your main bot and starts accepting VPS jobs. **This process must keep running** — see below. |
| **4. Exit** | Quits the menu without doing anything. |

**Typical first-time flow on the new server:**
```bash
sudo python3 node_agent.py
# choose 1 (Install VPS Bot) — wait for it to finish
sudo python3 node_agent.py
# choose 3 (Connect NODE) — paste the connect string — leave it running
```

### Keeping the node agent running 24/7

Choosing "Connect NODE" starts a foreground process — closing the terminal
stops it (and the node goes offline). Use either:

**tmux (quick):**
```bash
tmux new -s rivencloud-agent
sudo python3 node_agent.py     # choose 3, paste connect string
tmux attach -t rivencloud-agent # for alredy tmux
# Ctrl+B then D to detach — it keeps running
```

**systemd (recommended for production):** create `/etc/systemd/system/rivencloud-agent.service`:
```ini
[Unit]
Description=RIVEN CLOUD Node Agent
After=docker.service network.target

[Service]
Type=simple
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/node_agent.py
Restart=always
RestartSec=5
StandardInput=null

[Install]
WantedBy=multi-user.target
```
> Note: the systemd unit skips the interactive menu on restart only if
> `node_config.json` already exists from a prior "Connect NODE" run — run
> option 3 manually once first, confirm it connects, `Ctrl+C` it, **then**
> enable the systemd service so it reconnects automatically on boot/crash.

### Using a node

- **`/node-list`** — see every node and whether it's 🟢 online or 🔴 offline
- **`/deploy`** or **`/create`** — pick the node from the dropdown/menu when creating a VPS (or leave it on "Local" to use your main bot server)
- **`/node-delete name:Node1`** — removes a node record (blocked if it still has active VPS on it)

### ⚠️ Current limitation

Node support currently covers **VPS creation**. `/start`, `/stop`, `/restart`,
`/reinstall`, and `/remove-vps` currently act on the **local** Docker only —
for VPS hosted on a remote node, manage them directly on that node's machine
with the regular `docker` CLI for now (`docker stop <vps_id>`, etc.).

---

## 🔐 What the user receives

```
⚡ Your VPS is Ready
An admin deployed a VPS for you!

Instance ID       rivencloud-vps-0001
OS                Ubuntu 24.04
RAM / CPU         2g / 2 vCPU
Shared IPv4       <SERVER_IP or the node's public IP>
SSH Port (NAT)    <assigned port>
Username          root

Root Password
<16-character random password>

SSH Command
ssh root@<ip> -p <port>
```

---

## 🧪 Verifying your setup

Run **`/check-network`** (admin only) any time. It checks Docker reachability,
whether `.env` `SERVER_IP` matches this machine's real public IP, and whether
your SSH port range is free to bind locally.

From a **different machine**, confirm ports are actually reachable:
```bash
nc -zv <SERVER_IP> <ssh_port>
nc -zv <SERVER_IP> 8788        # AGENT_PORT, if testing a node connection
```

Then test the real SSH command from your own machine:
```bash
ssh root@<SERVER_IP> -p <port>
```

---

## 🛠 Troubleshooting

**"Docker socket not found"** — `sudo systemctl start docker`

**Users can't connect over SSH** — double-check your firewall **and** cloud
provider security group both allow the `SSH_PORT_START`–`SSH_PORT_END` range.

**A node stays 🔴 offline** — the agent process on that machine isn't running
(closed terminal, no tmux/systemd), the connect string was mistyped, or
`AGENT_PORT` isn't open on your **main bot server's** firewall.

**Bot doesn't respond to slash commands** — can take up to an hour to sync
globally the first time; try kicking and re-inviting the bot.

**"cgroupns not supported" warning** — safe to ignore; the bot automatically
falls back to a compatible container config.

---

## 📄 License

For personal/internal use. Modify freely for your own hosting community.


## Docker + Incus/LXC runtime modes

The updated `/create` command and `/deploy` modal accept `backend=docker` or
`backend=incus`.

- **docker** keeps the original Docker/systemd VPS runtime. This is not automatically registered as a Wings server by the current project.
- **incus** creates an actual Incus system container using LXC technology, sets
  memory/CPU/root-disk limits, installs OpenSSH and exposes root SSH through an
  Incus proxy device.
- Existing database rows are migrated automatically and remain `docker`.

### Incus host setup

Install and initialize Incus on the local bot host before selecting `incus`:

```bash
sudo apt update
sudo apt install -y incus
sudo systemctl enable --now incus
sudo incus admin init
incus list
```

On a remote node, run node-agent menu option 1 to install Docker, Incus and
dependencies. Verify `incus list` succeeds before deploying LXC VPS instances.
The Incus profile must provide a root disk device named `root` and a working
managed bridge/network. Open the configured SSH port range in both the host
firewall and cloud security group.

### Pterodactyl compatibility (important)

Pterodactyl Wings manages Docker-backed application/game servers; it does **not** natively manage Incus/LXC containers. The original uploaded project only queried the Panel API and had optional suspend/delete hooks for a pre-existing `ptero_id`; it did not provision Panel servers. This update preserves those hooks but does not invent a Panel server or claim that either runtime is automatically registered in Wings. Incus VPS instances are managed by this bot and Incus, not through the Panel console. To add actual Panel server creation, the Panel node, egg, nest, allocation, Docker image, startup command and Panel user mapping must be supplied and implemented as a separate Docker/Wings deployment path.

The original project only checked the Pterodactyl API and had suspend/delete calls
for an optional `ptero_id`; it did not create a Pterodactyl server as part of
provisioning. This update deliberately does not pretend that an LXC container is
a Pterodactyl server.
