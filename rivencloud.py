"""
╔═══════════════════════════════════════════════════════╗
║           RIVEN CLOUD VPS Manager Bot                  ║
║  Server: 180GB RAM | 94 Core CPU | Docker + systemd  ║
║  • Docker + Incus/LXC VPS runtimes                   ║
║  • Full systemctl support                            ║
║  • Direct root SSH (IP:port + password)              ║
║  • tmate SSH as backup access                        ║
║  • Fake neofetch specs                               ║
║  • Optional Pterodactyl API status/lifecycle hooks                         ║
║  • 1-click deploy                                    ║
╚═══════════════════════════════════════════════════════╝
"""

import os, io, time, socket, random, string, secrets, uuid, tarfile, asyncio, logging, sqlite3, datetime
import concurrent.futures, subprocess, ipaddress, shlex, shutil
import discord, docker, psutil, requests, aiohttp
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN", "")
ADMIN_ROLE_ID  = int(os.getenv("ADMIN_ROLE_ID", "0"))
ADMIN_USER_IDS = {
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",")
    if x.strip().isdigit()
}
PTERO_URL  = os.getenv("PTERO_URL", "").rstrip("/")
PTERO_KEY  = os.getenv("PTERO_API_KEY", "")
PTERO_ON   = bool(PTERO_URL and PTERO_KEY)

# Public IP of this host — shown to users as their "Shared IPv4".
# Set this in your .env, e.g. SERVER_IP=13.200.235.136
SERVER_IP      = os.getenv("SERVER_IP", "127.0.0.1")
SSH_PORT_START = int(os.getenv("SSH_PORT_START", "20000"))
SSH_PORT_END   = int(os.getenv("SSH_PORT_END", "29999"))

# Port the built-in node-agent WebSocket server listens on.
# Remote nodes connect OUTBOUND to ws://SERVER_IP:AGENT_PORT/agent/ws
# Open this port in your firewall (same as the SSH port range).
AGENT_PORT = int(os.getenv("AGENT_PORT", "8788"))

# ─────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("rivencloud.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("RIVEN CLOUD")

# ─────────────────────────────────────────────────────
# COLORS
# ─────────────────────────────────────────────────────
BLUE   = 0x5865F2
GREEN  = 0x57F287
RED    = 0xED4245
YELLOW = 0xFEE75C
DARK   = 0x2F3136
FOOTER = "Powered by RIVEN CLOUD"

# ─────────────────────────────────────────────────────
# OS + CPU
# ─────────────────────────────────────────────────────
# Using jrei/systemd images — pre-built for systemd inside Docker
# These support systemctl, services, cron out of the box
OS_MAP = {
    "ubuntu20": ("jrei/systemd-ubuntu:20.04", "Ubuntu 20.04"),
    "ubuntu22": ("jrei/systemd-ubuntu:22.04", "Ubuntu 22.04"),
    "ubuntu24": ("jrei/systemd-ubuntu:24.04", "Ubuntu 24.04"),
    "debian11":  ("jrei/systemd-debian:11",   "Debian 11"),
    "debian12":  ("jrei/systemd-debian:12",   "Debian 12"),
}
CPU_MAP = {
    "ryzen9": "AMD Ryzen 9 9950X 16-Core Processor",
    "xeon":   "Intel(R) Xeon(R) Platinum 8480+ @ 3.80GHz",
}

DB_FILE = "rivencloud.db"

# ─────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────
def get_db():
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with get_db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id  INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS vps (
                vps_id       TEXT    PRIMARY KEY,
                owner_id     INTEGER NOT NULL,
                container_id TEXT,
                os_image     TEXT,
                os_label     TEXT,
                ram_mb       INTEGER,
                cpu_cores    REAL,
                disk_gb      INTEGER,
                cpu_name     TEXT,
                ssh_cmd      TEXT    DEFAULT '',
                ssh_ip       TEXT    DEFAULT '',
                ssh_port     INTEGER DEFAULT NULL,
                root_pass    TEXT    DEFAULT '',
                username     TEXT    DEFAULT 'root',
                ptero_id     INTEGER DEFAULT NULL,
                status       TEXT    DEFAULT 'running',
                expires_at   TEXT    DEFAULT NULL,
                node_id      TEXT    DEFAULT NULL,
                backend      TEXT    DEFAULT 'docker',
                created_at   TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS nodes (
                node_id      TEXT    PRIMARY KEY,
                token        TEXT    NOT NULL,
                public_ip    TEXT    DEFAULT '',
                status       TEXT    DEFAULT 'offline',
                last_seen    TEXT    DEFAULT NULL,
                created_by   INTEGER,
                created_at   TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code         TEXT    PRIMARY KEY,
                ram_mb       INTEGER NOT NULL,
                cpu_cores    REAL    NOT NULL,
                disk_gb      INTEGER NOT NULL,
                valid_days   INTEGER DEFAULT 0,
                created_by   INTEGER,
                created_at   TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS port_forwards (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                vps_id       TEXT    NOT NULL,
                host_port    INTEGER NOT NULL UNIQUE,
                container_port INTEGER NOT NULL,
                protocol     TEXT    DEFAULT 'tcp',
                created_at   TEXT    DEFAULT (datetime('now'))
            );
        """)
        # Backfill columns for DBs created before this update
        cols = {row["name"] for row in c.execute("PRAGMA table_info(vps)").fetchall()}
        for col, ddl in [
            ("ssh_ip",       "ALTER TABLE vps ADD COLUMN ssh_ip TEXT DEFAULT ''"),
            ("ssh_port",     "ALTER TABLE vps ADD COLUMN ssh_port INTEGER DEFAULT NULL"),
            ("root_pass",    "ALTER TABLE vps ADD COLUMN root_pass TEXT DEFAULT ''"),
            ("username",     "ALTER TABLE vps ADD COLUMN username TEXT DEFAULT 'root'"),
            ("node_id",      "ALTER TABLE vps ADD COLUMN node_id TEXT DEFAULT NULL"),
            ("backend",      "ALTER TABLE vps ADD COLUMN backend TEXT DEFAULT 'docker'"),
        ]:
            if col not in cols:
                c.execute(ddl)
    log.info("Database ready.")

# ─────────────────────────────────────────────────────
# PORT + PASSWORD HELPERS
# ─────────────────────────────────────────────────────
def _port_in_use_locally(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0

def find_free_port() -> int:
    """Pick a host port not already assigned to another VPS and not in use."""
    with get_db() as c:
        used = {row["ssh_port"] for row in
                c.execute("SELECT ssh_port FROM vps WHERE ssh_port IS NOT NULL").fetchall()}
    for _ in range(200):
        p = random.randint(SSH_PORT_START, SSH_PORT_END)
        if p in used:
            continue
        if _port_in_use_locally(p):
            continue
        return p
    raise RuntimeError("No free SSH ports available in range.")

def gen_root_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(length))


def incus_run(*args, timeout=900, check=True):
    if shutil.which("incus") is None:
        raise RuntimeError("Incus is not installed. Install it and run `incus admin init`.")
    p = subprocess.run(["incus", *map(str, args)], text=True, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "Incus command failed").strip())
    return p

def incus_image_for(image):
    return {
        "jrei/systemd-ubuntu:20.04": "images:ubuntu/20.04",
        "jrei/systemd-ubuntu:22.04": "images:ubuntu/22.04",
        "jrei/systemd-ubuntu:24.04": "images:ubuntu/24.04",
        "jrei/systemd-debian:11": "images:debian/11",
        "jrei/systemd-debian:12": "images:debian/12",
    }.get(image, image)

def provision_lxc(vps_id, image, os_label, ram_mb, cpu_cores, disk_gb, cpu_name, host_port, root_pass):
    if _port_in_use_locally(host_port):
        raise RuntimeError(f"SSH port {host_port} is already in use.")
    open_port_firewall(host_port)
    incus_run("launch", incus_image_for(image), vps_id,
              "--config", f"limits.memory={int(ram_mb)}MiB",
              "--config", f"limits.cpu={max(1, int(float(cpu_cores) + 0.999))}",
              "--config", f"limits.cpu.allowance={float(cpu_cores) * 100:g}%")
    try:
        incus_run("config", "device", "set", vps_id, "root", "size", f"{int(disk_gb)}GiB")
        setup = (
            "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && "
            "apt-get install -y -qq openssh-server tmate sudo curl wget procps iproute2 net-tools htop; "
            f"echo {shlex.quote('root:'+root_pass)} | chpasswd; "
            "mkdir -p /run/sshd; "
            "sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config; "
            "sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
            "grep -q '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config; "
            "grep -q '^PasswordAuthentication' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config; "
            "systemctl enable ssh; systemctl restart ssh"
        )
        incus_run("exec", vps_id, "--", "bash", "-lc", setup)
        guest_ip = incus_run("exec", vps_id, "--", "bash", "-lc", "hostname -I").stdout.split()
        guest_ip = next((ip for ip in guest_ip if ":" not in ip), "")
        if not guest_ip:
            raise RuntimeError("Incus container has no IPv4 address; check the Incus bridge/profile.")
        incus_run("config", "device", "add", vps_id, "rivencloud-ssh", "proxy",
                  f"listen=tcp:0.0.0.0:{host_port}", f"connect=tcp:{guest_ip}:22", "bind=host")
        incus_run("config", "set", vps_id, "user.rivencloud.managed", "true")
    except Exception:
        incus_run("delete", vps_id, "--force", check=False)
        raise
    backup_ssh = incus_regen_ssh(vps_id)
    return vps_id, backup_ssh

def incus_action(vps_id, action):
    actions = {"start": ["start"], "stop": ["stop"], "restart": ["restart"], "remove": ["delete", "--force"]}
    if action not in actions:
        raise RuntimeError(f"Unsupported Incus action: {action}")
    incus_run(*actions[action], vps_id, timeout=300)

def incus_regen_ssh(vps_id):
    incus_run("exec", vps_id, "--", "bash", "-lc",
              "command -v tmate >/dev/null || (apt-get update -qq && apt-get install -y -qq tmate); "
              "pkill tmate 2>/dev/null || true; rm -f /tmp/tmate.sock; "
              "tmate -S /tmp/tmate.sock new-session -d; tmate -S /tmp/tmate.sock wait tmate-ready")
    return incus_run("exec", vps_id, "--", "bash", "-lc",
                     "tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'", timeout=60).stdout.strip()


def find_free_port_for_node(node_id: str) -> int:
    """Like find_free_port(), but for a remote node — we can't locally
    bind-test a port on another machine, so we only avoid collisions
    with ports we've already handed out on that same node."""
    with get_db() as c:
        used = {row["ssh_port"] for row in c.execute(
            "SELECT ssh_port FROM vps WHERE node_id=? AND ssh_port IS NOT NULL", (node_id,)
        ).fetchall()}
    for _ in range(200):
        p = random.randint(SSH_PORT_START, SSH_PORT_END)
        if p not in used:
            return p
    raise RuntimeError("No free SSH ports available in range for this node.")

def gen_redeem_code() -> str:
    part = lambda: "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"SN-{part()}-{part()}-{part()}"


def open_port_firewall(port: int):
    """Auto-open this port in iptables so SSH doesn't time out."""
    try:
        subprocess.run(
            ["iptables", "-I", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"],
            capture_output=True, check=False,
        )
        log.info(f"iptables: opened port {port}")
    except Exception as e:
        log.warning(f"iptables open failed (non-fatal): {e}")


# ─────────────────────────────────────────────────────
# PORT FORWARDING HELPERS
# ─────────────────────────────────────────────────────
PORT_FWD_MAX = 10   # max port forwards per VPS

def _get_container_ip(container_id: str) -> str:
    """Get the internal Docker IP of a running container."""
    try:
        ct = get_docker().containers.get(container_id)
        ct.reload()
        nets = ct.attrs["NetworkSettings"]["Networks"]
        for net in nets.values():
            ip = net.get("IPAddress", "")
            if ip:
                return ip
    except Exception:
        pass
    return ""

def add_port_forward(vps_id: str, container_id: str, host_port: int, container_port: int) -> str:
    """Add iptables DNAT rule to forward host_port → container:container_port."""
    with get_db() as c:
        count = c.execute(
            "SELECT COUNT(*) AS n FROM port_forwards WHERE vps_id=?", (vps_id,)
        ).fetchone()["n"]
        if count >= PORT_FWD_MAX:
            raise RuntimeError(f"Maximum {PORT_FWD_MAX} port forwards per VPS reached.")
        used = c.execute(
            "SELECT 1 FROM port_forwards WHERE host_port=?", (host_port,)
        ).fetchone()
        if used:
            raise RuntimeError(f"Host port {host_port} is already in use by another forward.")

    container_ip = _get_container_ip(container_id)
    if not container_ip:
        raise RuntimeError("Could not determine container IP. Is the VPS running?")

    # Open host firewall port
    subprocess.run(["iptables", "-I", "INPUT", "-p", "tcp",
                    "--dport", str(host_port), "-j", "ACCEPT"],
                   capture_output=True, check=False)
    # DNAT: external host_port → container_ip:container_port
    subprocess.run(["iptables", "-t", "nat", "-A", "PREROUTING",
                    "-p", "tcp", "--dport", str(host_port),
                    "-j", "DNAT", "--to-destination", f"{container_ip}:{container_port}"],
                   capture_output=True, check=False)
    # Allow forwarded traffic
    subprocess.run(["iptables", "-A", "FORWARD", "-p", "tcp",
                    "-d", container_ip, "--dport", str(container_port), "-j", "ACCEPT"],
                   capture_output=True, check=False)

    with get_db() as c:
        c.execute(
            "INSERT INTO port_forwards (vps_id, host_port, container_port) VALUES (?,?,?)",
            (vps_id, host_port, container_port),
        )
    log.info(f"[{vps_id}] Port forward added: {host_port} → {container_ip}:{container_port}")
    return container_ip

def remove_port_forward(vps_id: str, container_id: str, host_port: int):
    """Remove iptables DNAT rule and DB record."""
    with get_db() as c:
        row = c.execute(
            "SELECT * FROM port_forwards WHERE vps_id=? AND host_port=?", (vps_id, host_port)
        ).fetchone()
    if not row:
        raise RuntimeError(f"No port forward found for host port {host_port} on {vps_id}.")

    container_ip = _get_container_ip(container_id)
    container_port = row["container_port"]

    if container_ip:
        subprocess.run(["iptables", "-t", "nat", "-D", "PREROUTING",
                        "-p", "tcp", "--dport", str(host_port),
                        "-j", "DNAT", "--to-destination", f"{container_ip}:{container_port}"],
                       capture_output=True, check=False)
        subprocess.run(["iptables", "-D", "FORWARD", "-p", "tcp",
                        "-d", container_ip, "--dport", str(container_port), "-j", "ACCEPT"],
                       capture_output=True, check=False)
    subprocess.run(["iptables", "-D", "INPUT", "-p", "tcp",
                    "--dport", str(host_port), "-j", "ACCEPT"],
                   capture_output=True, check=False)

    with get_db() as c:
        c.execute("DELETE FROM port_forwards WHERE vps_id=? AND host_port=?", (vps_id, host_port))
    log.info(f"[{vps_id}] Port forward removed: host_port={host_port}")



# ─────────────────────────────────────────────────────
# EMBED HELPER
# ─────────────────────────────────────────────────────
def em(title, desc="", color=BLUE, fields=None):
    e = discord.Embed(
        title=title, description=desc,
        color=color, timestamp=datetime.datetime.utcnow()
    )
    e.set_footer(text=FOOTER)
    for n, v, i in (fields or []):
        e.add_field(name=n, value=v, inline=i)
    return e

# ─────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────
def get_docker():
    try:
        client = docker.from_env()
        client.ping()
        return client
    except FileNotFoundError:
        raise RuntimeError(
            "Docker socket not found!\n"
            "Run: `sudo systemctl start docker`"
        )
    except docker.errors.DockerException as e:
        raise RuntimeError(f"Docker error: {e}\nRun: `sudo systemctl start docker`")

def is_admin(ix: discord.Interaction) -> bool:
    if ix.user.id in ADMIN_USER_IDS:
        return True
    if ix.guild:
        return any(r.id == ADMIN_ROLE_ID for r in ix.user.roles)
    return False

def owns(uid: int, vid: str) -> bool:
    with get_db() as c:
        return bool(c.execute(
            "SELECT 1 FROM vps WHERE vps_id=? AND owner_id=?", (vid, uid)
        ).fetchone())

# ─────────────────────────────────────────────────────
# NODE MANAGER — WebSocket RPC to remote node agents
# ─────────────────────────────────────────────────────
# Remote nodes run node_agent.py and connect OUTBOUND to this bot's
# ws://SERVER_IP:AGENT_PORT/agent/ws — no inbound port needed on the
# node's side, only on this main bot's machine (same firewall rule
# you already opened for SSH_PORT_START-SSH_PORT_END).
NODE_CONNECTIONS: dict[str, web.WebSocketResponse] = {}
PENDING_JOBS: dict[str, "asyncio.Future"] = {}

def node_is_online(node_id: str) -> bool:
    return node_id in NODE_CONNECTIONS

async def send_job_to_node(node_id: str, job: dict, timeout: int = 180) -> dict:
    """Send a job to a connected node agent and await its JSON result."""
    ws = NODE_CONNECTIONS.get(node_id)
    if ws is None:
        raise RuntimeError(f"Node '{node_id}' is offline.")
    job_id = str(uuid.uuid4())
    job["job_id"] = job_id
    fut = asyncio.get_running_loop().create_future()
    PENDING_JOBS[job_id] = fut
    try:
        await ws.send_json(job)
        return await asyncio.wait_for(fut, timeout=timeout)
    finally:
        PENDING_JOBS.pop(job_id, None)

async def ws_agent_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    node_id = None
    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = msg.json()
            mtype = data.get("type")

            if mtype == "hello":
                nid, token = data.get("node_id"), data.get("token")
                with get_db() as c:
                    row = c.execute(
                        "SELECT * FROM nodes WHERE node_id=?", (nid,)
                    ).fetchone()
                if not row or row["token"] != token:
                    await ws.send_json({"type": "hello_ack", "ok": False, "error": "Invalid node_id/token."})
                    await ws.close()
                    return ws
                node_id = nid
                peer_ip = request.remote or ""
                with get_db() as c:
                    c.execute(
                        "UPDATE nodes SET status='online', public_ip=?, last_seen=datetime('now') WHERE node_id=?",
                        (peer_ip, node_id),
                    )
                NODE_CONNECTIONS[node_id] = ws
                log.info(f"[node:{node_id}] connected from {peer_ip}")
                await ws.send_json({"type": "hello_ack", "ok": True})

            elif mtype == "job_result":
                fut = PENDING_JOBS.get(data.get("job_id"))
                if fut and not fut.done():
                    fut.set_result(data)

            elif mtype == "heartbeat" and node_id:
                with get_db() as c:
                    c.execute("UPDATE nodes SET last_seen=datetime('now') WHERE node_id=?", (node_id,))
    finally:
        if node_id:
            NODE_CONNECTIONS.pop(node_id, None)
            with get_db() as c:
                c.execute("UPDATE nodes SET status='offline' WHERE node_id=?", (node_id,))
            log.info(f"[node:{node_id}] disconnected")
    return ws

async def start_agent_server():
    app = web.Application()
    app.router.add_get("/agent/ws", ws_agent_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", AGENT_PORT, reuse_address=True)
    try:
        await site.start()
        log.info(f"Node-agent WebSocket server listening on 0.0.0.0:{AGENT_PORT}")
    except OSError as e:
        if e.errno == 98:  # Address already in use
            log.error(
                f"❌ Port {AGENT_PORT} is already in use — the node-agent server "
                f"did NOT start (Discord bot will still run normally otherwise).\n"
                f"   This usually means an old copy of this bot is still running. Fix with:\n"
                f"     sudo lsof -i :{AGENT_PORT}      # find the PID using this port\n"
                f"     sudo kill -9 <PID>              # stop it\n"
                f"   Or set a different AGENT_PORT in your .env and restart."
            )
        else:
            log.error(f"❌ Node-agent server failed to start: {e}")

def next_id() -> str:
    with get_db() as c:
        row = c.execute("SELECT vps_id FROM vps ORDER BY vps_id DESC LIMIT 1").fetchone()
    db_num = 1 if not row else int(row["vps_id"].split("-")[-1]) + 1
    dk_max = 0
    try:
        for ct in get_docker().containers.list(
            all=True, filters={"label": "managed-by=rivencloud"}
        ):
            if ct.name.startswith("rivencloud-vps-"):
                try:
                    dk_max = max(dk_max, int(ct.name.split("-")[-1]))
                except ValueError:
                    pass
    except Exception:
        pass
    return f"rivencloud-vps-{max(db_num, dk_max + 1):04d}"

def gb(b): return round(b / 1024**3, 2)

# ─────────────────────────────────────────────────────
# PTERODACTYL
# ─────────────────────────────────────────────────────
def ph():
    return {
        "Authorization": f"Bearer {PTERO_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def ptero_get(ep):
    r = requests.get(f"{PTERO_URL}/api/application/{ep}", headers=ph(), timeout=10)
    r.raise_for_status()
    return r.json()

def ptero_post(ep, data=None):
    r = requests.post(f"{PTERO_URL}/api/application/{ep}", headers=ph(), json=data or {}, timeout=10)
    r.raise_for_status()
    return r.json() if r.text.strip() else {}

def ptero_delete(ep):
    requests.delete(f"{PTERO_URL}/api/application/{ep}", headers=ph(), timeout=10).raise_for_status()

def ptero_check():
    try:
        n = ptero_get("nodes")
        return {"ok": True, "nodes": len(n.get("data", []))}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def ptero_suspend(pid):   ptero_post(f"servers/{pid}/suspend")
def ptero_unsuspend(pid): ptero_post(f"servers/{pid}/unsuspend")
def ptero_remove(pid):    ptero_delete(f"servers/{pid}/force")

# ─────────────────────────────────────────────────────
# FAKE /proc GENERATORS
# ─────────────────────────────────────────────────────
def fake_meminfo(mb: int) -> str:
    kb = mb * 1024
    return "\n".join([
        f"MemTotal:       {kb} kB",
        f"MemFree:        {int(kb*.88)} kB",
        f"MemAvailable:   {int(kb*.85)} kB",
        "Buffers:            128 kB",
        f"Cached:         {int(kb*.05)} kB",
        "SwapCached:           0 kB",
        f"Active:         {int(kb*.10)} kB",
        f"Inactive:       {int(kb*.02)} kB",
        "SwapTotal:            0 kB",
        "SwapFree:             0 kB",
        "Dirty:                4 kB",
        "Writeback:            0 kB",
        f"AnonPages:      {int(kb*.08)} kB",
        f"Mapped:         {int(kb*.02)} kB",
        "Shmem:               64 kB",
        "Slab:               512 kB",
        f"VmallocTotal:   {kb} kB",
        "VmallocUsed:          0 kB",
        f"VmallocChunk:   {kb} kB",
        "HugePages_Total:      0",
        "HugePages_Free:       0",
        "Hugepagesize:      2048 kB", "",
    ])

def fake_cpuinfo(cores: float, name: str) -> str:
    n = max(1, int(cores))
    v = "AuthenticAMD" if ("AMD" in name or "Ryzen" in name) else "GenuineIntel"
    blocks = []
    for i in range(n):
        blocks.append("\n".join([
            f"processor\t: {i}",
            f"vendor_id\t: {v}",
            "cpu family\t: 25",
            "model\t\t: 97",
            f"model name\t: {name}",
            "stepping\t: 2",
            "cpu MHz\t\t: 4200.000",
            "cache size\t: 65536 KB",
            "physical id\t: 0",
            f"siblings\t: {n}",
            f"core id\t\t: {i}",
            f"cpu cores\t: {n}",
            "fpu\t\t: yes",
            "bogomips\t: 8400.00",
            "clflush size\t: 64",
            "cache_alignment\t: 64", "",
        ]))
    return "\n".join(blocks)

def write_file(ct, path: str, content: str):
    data = content.encode()
    buf  = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ti = tarfile.TarInfo(name=os.path.basename(path))
        ti.size = len(data)
        ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    ct.put_archive(os.path.dirname(path) or "/", buf)

# ─────────────────────────────────────────────────────
# CORE VPS PROVISION
# ─────────────────────────────────────────────────────
def provision(vps_id, image, os_label, ram_mb, cpu_cores, disk_gb, cpu_name,
              host_port, root_pass) -> tuple:
    """
    Creates a Docker VPS with full systemd support.

    Steps:
      1. Pull jrei/systemd image
      2. Create container on rivencloud-net (unique IP per VPS)
      3. Wait for systemd to boot
      4. apt install openssh-server (no update — faster)
      5. Fake /proc/meminfo and /proc/cpuinfo
      6. Set hostname and MOTD
      7. Generate SSH host keys + enable password SSH login
      8. Auto-open firewall port via iptables
      9. Start tmate SSH session (45s timeout)
    """
    client  = get_docker()
    mem     = f"{ram_mb}m"
    period  = 100_000
    quota   = int(period * cpu_cores)

    log.info(f"[{vps_id}] Provisioning — RAM:{ram_mb}MB CPU:{cpu_cores} Disk:{disk_gb}GB")

    # ── Step 1: Remove any leftover container ───────────────────────
    try:
        for old in client.containers.list(all=True, filters={"name": f"^/{vps_id}$"}):
            log.warning(f"[{vps_id}] Removing leftover {old.short_id}")
            try: old.remove(force=True, v=True)
            except Exception: pass
        for _ in range(10):
            if not client.containers.list(all=True, filters={"name": f"^/{vps_id}$"}):
                break
            time.sleep(1)
    except Exception as e:
        log.warning(f"[{vps_id}] Cleanup warning: {e}")

    # ── Step 2: Pull jrei/systemd image ─────────────────────────────
    log.info(f"[{vps_id}] Pulling {image}...")

    # Auto-open firewall port so users can connect without manual steps
    open_port_firewall(host_port)
    try:
        client.images.pull(image)
        log.info(f"[{vps_id}] Image ready: {image}")
    except Exception as e:
        raise RuntimeError(
            f"Failed to pull `{image}`.\n"
            f"Check internet connection on your server.\nError: {e}"
        )

    # ── Step 3: Create container via low-level API ──────────────────
    log.info(f"[{vps_id}] Creating container with CgroupnsMode=host...")


    try:
        host_cfg = client.api.create_host_config(
            mem_limit=mem,
            memswap_limit=mem,
            cpu_period=period,
            cpu_quota=quota,
            privileged=True,
            cgroupns="host",
            binds={"/sys/fs/cgroup": {"bind": "/sys/fs/cgroup", "mode": "rw"}},
            tmpfs={
                "/run":      "rw,nosuid,nodev",
                "/run/lock": "rw,nosuid,nodev",
                "/tmp":      "rw,nosuid,nodev",
            },
            port_bindings={22: host_port},
        )
    except TypeError:
        log.warning(f"[{vps_id}] cgroupns not supported in this docker-py, trying without...")
        host_cfg = client.api.create_host_config(
            mem_limit=mem,
            memswap_limit=mem,
            cpu_period=period,
            cpu_quota=quota,
            privileged=True,
            binds={"/sys/fs/cgroup": {"bind": "/sys/fs/cgroup", "mode": "rw"}},
            tmpfs={
                "/run":      "rw,nosuid,nodev",
                "/run/lock": "rw,nosuid,nodev",
                "/tmp":      "rw,nosuid,nodev",
            },
            port_bindings={22: host_port},
        )

    ct_data = client.api.create_container(
        image=image,
        name=vps_id,
        detach=True,
        tty=True,
        stdin_open=True,
        environment={"TERM": "xterm-256color", "container": "docker"},
        command="/sbin/init",
        host_config=host_cfg,
        ports=[22],
        labels={"managed-by": "rivencloud", "vps-id": vps_id},
    )
    client.api.start(ct_data["Id"])
    ct = client.containers.get(ct_data["Id"])
    log.info(f"[{vps_id}] Container started: {ct.short_id}")

    # ── Step 4: Wait for systemd to fully boot ───────────────────────
    log.info(f"[{vps_id}] Waiting for systemd to initialize...")
    time.sleep(6)

    # ── Step 5: apt update + install packages ───────────────────────
    log.info(f"[{vps_id}] Running apt update...")
    ct.exec_run("bash -c 'apt-get update -qq 2>&1'", tty=False)
    log.info(f"[{vps_id}] Installing openssh-server, tools...")
    ct.exec_run(
        "bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "openssh-server neofetch curl wget sudo procps net-tools iproute2 htop'",
        tty=False,
    )

    # ── Step 6: VERIFY openssh-server installed — retry if not ──────
    for attempt in range(1, 4):
        r = ct.exec_run("bash -c 'which sshd || ls /usr/sbin/sshd 2>/dev/null'", tty=False)
        sshd_exists = r.output and b"sshd" in r.output
        log.info(f"[{vps_id}] sshd binary check attempt {attempt}: exists={sshd_exists}")
        if sshd_exists:
            break
        log.warning(f"[{vps_id}] openssh-server not found, retrying install (attempt {attempt})...")
        ct.exec_run("bash -c 'apt-get update -qq 2>&1'", tty=False)
        ct.exec_run(
            "bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server 2>&1'",
            tty=False,
        )
    else:
        log.error(f"[{vps_id}] FAILED to install openssh-server after 3 attempts!")
        raise RuntimeError(
            "openssh-server could not be installed in the container after 3 attempts. "
            "Check the host's internet connection and Docker apt cache."
        )

    # ── Step 7: Fake /proc/meminfo and /proc/cpuinfo ─────────────────
    ct.exec_run("mkdir -p /etc/rivencloud", tty=False)

    write_file(ct, "/etc/rivencloud/meminfo", fake_meminfo(ram_mb))
    r = ct.exec_run("mount --bind /etc/rivencloud/meminfo /proc/meminfo", tty=False)
    log.info(f"[{vps_id}] meminfo bind mount: exit={r.exit_code}")

    write_file(ct, "/etc/rivencloud/cpuinfo", fake_cpuinfo(cpu_cores, cpu_name))
    r = ct.exec_run("mount --bind /etc/rivencloud/cpuinfo /proc/cpuinfo", tty=False)
    log.info(f"[{vps_id}] cpuinfo bind mount: exit={r.exit_code}")

    # Re-apply mounts on container restart
    write_file(ct, "/etc/rc.local",
        "#!/bin/bash\n"
        "mount --bind /etc/rivencloud/meminfo /proc/meminfo 2>/dev/null\n"
        "mount --bind /etc/rivencloud/cpuinfo /proc/cpuinfo 2>/dev/null\n"
        "exit 0\n"
    )
    ct.exec_run("chmod +x /etc/rc.local", tty=False)

    # ── Step 8: Hostname + MOTD ──────────────────────────────────────
    ci = int(cpu_cores) if cpu_cores == int(cpu_cores) else cpu_cores
    ct.exec_run(
        f"bash -c 'hostnamectl set-hostname {vps_id} 2>/dev/null || hostname {vps_id}'",
        tty=False,
    )
    ct.exec_run(f"bash -c 'echo {vps_id} > /etc/hostname'", tty=False)
    write_file(ct, "/etc/motd",
        f"\n"
        f"  ╔══════════════════════════════════╗\n"
        f"  ║        🐉  RIVEN CLOUD VPS           ║\n"
        f"  ╠══════════════════════════════════╣\n"
        f"  ║  VPS ID : {vps_id:<24}║\n"
        f"  ║  RAM    : {str(ram_mb)+' MB':<24}║\n"
        f"  ║  CPU    : {str(ci)+' vCore(s)':<24}║\n"
        f"  ║  Disk   : {str(disk_gb)+' GB':<24}║\n"
        f"  ║  OS     : {os_label:<24}║\n"
        f"  ╚══════════════════════════════════╝\n\n"
    )

    # ── Step 9: Root password + bulletproof SSH setup ────────────────
    log.info(f"[{vps_id}] Setting root password and configuring SSH...")

    # Set root password
    ct.exec_run(f"bash -c \"echo 'root:{root_pass}' | chpasswd\"", tty=False)

    # Create required directories
    ct.exec_run("mkdir -p /run/sshd /var/run/sshd /etc/ssh", tty=False)

    # Generate SSH host keys
    r = ct.exec_run("ssh-keygen -A 2>&1", tty=False)
    log.info(f"[{vps_id}] ssh-keygen -A exit={r.exit_code}")

    # Write fresh minimal sshd_config via write_file
    # UsePrivilegeSeparation no + UsePAM no are CRITICAL for Docker
    sshd_config = (
        "Port 22\n"
        "Protocol 2\n"
        "HostKey /etc/ssh/ssh_host_rsa_key\n"
        "HostKey /etc/ssh/ssh_host_ecdsa_key\n"
        "HostKey /etc/ssh/ssh_host_ed25519_key\n"
        "UsePrivilegeSeparation no\n"
        "PermitRootLogin yes\n"
        "PasswordAuthentication yes\n"
        "ChallengeResponseAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "UsePAM no\n"
        "X11Forwarding no\n"
        "PrintMotd yes\n"
        "AcceptEnv LANG LC_*\n"
        "Subsystem sftp /usr/lib/openssh/sftp-server\n"
    )
    write_file(ct, "/etc/ssh/sshd_config", sshd_config)

    # Test config
    r = ct.exec_run("sshd -t 2>&1", tty=False)
    out = r.output.decode(errors="ignore").strip() if r.output else ""
    log.info(f"[{vps_id}] sshd -t exit={r.exit_code} | {out}")

    # Kill old sshd, start fresh directly — bypass systemctl for reliability
    ct.exec_run("bash -c 'pkill -9 sshd 2>/dev/null; sleep 1'", tty=False)
    r = ct.exec_run("bash -c '/usr/sbin/sshd -f /etc/ssh/sshd_config 2>&1'", tty=False)
    out = r.output.decode(errors="ignore").strip() if r.output else ""
    log.info(f"[{vps_id}] sshd start exit={r.exit_code} | {out}")

    # Verify sshd is listening
    time.sleep(3)
    r2 = ct.exec_run("bash -c 'ss -tlnp | grep :22'", tty=False)
    sshd_up = r2.output and b":22" in r2.output
    log.info(f"[{vps_id}] sshd listening :22 = {sshd_up}")
    if not sshd_up:
        ct.exec_run("bash -c '/usr/sbin/sshd -f /etc/ssh/sshd_config'", tty=False)
        time.sleep(2)
        r3 = ct.exec_run("bash -c 'ss -tlnp | grep :22'", tty=False)
        log.info(f"[{vps_id}] sshd retry listening = {r3.output and b':22' in r3.output}")

    log.info(f"[{vps_id}] ✅ Provision complete — direct SSH ready on port {host_port}")
    return ct, ""


def regen_ssh(ct) -> str:
    sock = "/tmp/tmate.sock"
    ct.exec_run("bash -c 'pkill tmate; rm -f /tmp/tmate.sock'", tty=False)
    time.sleep(2)
    ct.exec_run(f"bash -c 'tmate -S {sock} new-session -d'", tty=False)

    def _wait():
        ct.exec_run(f"bash -c 'tmate -S {sock} wait tmate-ready'", tty=False)
        r = ct.exec_run(f"bash -c \"tmate -S {sock} display -p '#{{tmate_ssh}}'\"", tty=False)
        return r.output.decode(errors="ignore").strip() if r.output else ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_wait)
        try:
            return fut.result(timeout=45)
        except concurrent.futures.TimeoutError:
            log.warning("regen_ssh: tmate timed out after 45s")
            return ""


def get_stats(ct, ram_mb=0, cores=0) -> dict:
    raw = ct.stats(stream=False)
    cd  = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
    sd  = raw["cpu_stats"]["system_cpu_usage"] - raw["precpu_stats"]["system_cpu_usage"]
    nc  = raw["cpu_stats"].get("online_cpus", 1)
    rp  = (cd / sd) * nc * 100 if sd else 0
    cpu = round(min(rp / cores, 100), 2) if cores else round(rp, 2)
    mu  = raw["memory_stats"].get("usage", 0)
    ml  = ram_mb * 1024 * 1024 if ram_mb else 1
    rx = tx = 0
    for iface in raw.get("networks", {}).values():
        rx += iface.get("rx_bytes", 0)
        tx += iface.get("tx_bytes", 0)
    started = ct.attrs["State"].get("StartedAt", "")
    up = "N/A"
    if started and started != "0001-01-01T00:00:00Z":
        try:
            s = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
            d = datetime.datetime.now(datetime.timezone.utc) - s
            h, r2 = divmod(int(d.total_seconds()), 3600)
            m, s2 = divmod(r2, 60)
            up = f"{h}h {m}m {s2}s"
        except Exception: pass
    return {
        "cpu":    cpu,
        "mem_mb": round(mu / 1024 / 1024, 1),
        "mem_p":  round(min(mu / ml * 100, 100), 2),
        "rx":     round(rx / 1024 / 1024, 2),
        "tx":     round(tx / 1024 / 1024, 2),
        "up":     up,
    }

# ─────────────────────────────────────────────────────
# SHARED CREATE LOGIC
# ─────────────────────────────────────────────────────
async def do_create(ix, user, ram, cpu, disk, os_key, cpu_key, days=0, node_id=None, backend='docker'):
    image, os_label = OS_MAP[os_key]
    cpu_name        = CPU_MAP[cpu_key]
    backend = (backend or 'docker').lower()
    if backend not in ('docker', 'incus'):
        return await ix.followup.send(embed=em('Invalid runtime', 'Use docker or incus.', RED))
    vps_id          = next_id()

    if node_id and not node_is_online(node_id):
        return await ix.followup.send(embed=em(
            "❌ Node Offline",
            f"Node **{node_id}** is not connected right now. Pick another node "
            f"or run `/node-list` to check status.",
            RED,
        ))

    exp_at   = None
    exp_note = "Never expires"
    if days > 0:
        dt       = datetime.datetime.utcnow() + datetime.timedelta(days=days)
        exp_at   = dt.isoformat()
        exp_note = f"Auto-suspends <t:{int(dt.timestamp())}:R>"

    await ix.followup.send(embed=em(
        "⏳ Provisioning VPS...",
        f"**{vps_id}** for {user.mention}\n\n"
        "```\n"
        "[1/4] Pulling image               ⏳\n"
        "[2/4] Creating container          ⏳\n"
        "[3/4] apt update + apt install    ⏳\n"
        "[4/4] Starting tmate SSH          ⏳\n"
        "```\n"
        "⏱ ~90 seconds — SSH sent to DM.",
        BLUE,
        fields=[
            ("🖥 OS",        os_label,                          True),
            ("🧠 RAM",       f"{ram} MB",                       True),
            ("💻 CPU",       f"{cpu} Core(s)",                  True),
            ("💾 Disk",      f"{disk} GB",                      True),
            ("🏷 CPU Model", cpu_name,                          False),
            ("📡 Node",      node_id or "N1 (This Server)",  False),
            ("⏰ Expiry",    exp_note,                          False),
        ],
    ))

    root_pass = gen_root_password()
    ssh_ip    = SERVER_IP

    try:
        if node_id:
            host_port = find_free_port_for_node(node_id)
            result = await send_job_to_node(node_id, {
                "type": ("create_lxc_vps" if backend == "incus" else "create_vps"), "backend": backend, "vps_id": vps_id, "image": image,
                "os_label": os_label, "ram_mb": ram, "cpu_cores": cpu,
                "disk_gb": disk, "cpu_name": cpu_name,
                "host_port": host_port, "root_pass": root_pass,
            })
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "Unknown node error"))
            container_id = result.get("container_id", "")
            ssh          = result.get("ssh", "")
            host_port    = result.get("host_port", host_port)
            with get_db() as c:
                row = c.execute("SELECT public_ip FROM nodes WHERE node_id=?", (node_id,)).fetchone()
                ssh_ip = row["public_ip"] if row and row["public_ip"] else SERVER_IP
        else:
            host_port = find_free_port()
            loop = asyncio.get_running_loop()
            if backend == "incus":
                container_id, ssh = await loop.run_in_executor(
                    None, lambda: provision_lxc(vps_id, image, os_label, ram, cpu, disk,
                                                cpu_name, host_port, root_pass))
            else:
                ct, ssh = await loop.run_in_executor(
                    None, lambda: provision(vps_id, image, os_label, ram, cpu, disk, cpu_name,
                                            host_port, root_pass))
                container_id = ct.id
    except Exception as e:
        log.error(f"[{vps_id}] Failed: {e}")
        if not node_id:
            try:
                if backend == "incus":
                    incus_run("delete", vps_id, "--force", check=False)
                else:
                    get_docker().containers.get(vps_id).remove(force=True, v=True)
            except Exception:
                pass
        return await ix.followup.send(embed=em(
            "❌ Provisioning Failed",
            f"**{vps_id}** could not be created.\n```{str(e)[:600]}```\n"
            + (f"Run `/fix-vps {vps_id}` then try again." if not node_id else "Check the node's agent logs."),
            RED,
        ))

    with get_db() as c:
        c.execute("""
            INSERT INTO vps
              (vps_id,owner_id,container_id,os_image,os_label,
               ram_mb,cpu_cores,disk_gb,cpu_name,ssh_cmd,
               ssh_ip,ssh_port,root_pass,username,status,expires_at,node_id,backend)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'root','running',?,?,?)
        """, (vps_id, user.id, container_id, image, os_label,
              ram, cpu, disk, cpu_name, ssh,
              ssh_ip, host_port, root_pass, exp_at, node_id, backend))

    log.info(f"Created {vps_id} for {user} by {ix.user} on node={node_id or 'local'}")

    ram_gb   = ram / 1024
    ram_disp = f"{ram_gb:g}g" if ram_gb == int(ram_gb) else f"{ram_gb:.1f}g"
    cpu_disp = f"{cpu:g}" if float(cpu) == int(cpu) else f"{cpu}"
    direct_ssh_cmd = f"ssh root@{ssh_ip} -p {host_port}"

    # ── Send DM ───────────────────────────────────────────────────────
    dm_ok = False
    try:
        log.info(f"[{vps_id}] Sending DM to {user}...")
        fields = [
            ("Instance ID",    f"`{vps_id}`",                    True),
            ("OS",              os_label,                         True),
            ("RAM / CPU",       f"{ram_disp} / {cpu_disp} vCPU", True),
            ("Shared IPv4",     f"`{ssh_ip}`",                    True),
            ("SSH Port (NAT)",  f"`{host_port}`",                 True),
            ("Username",        "`root`",                         True),
            ("Root Password",   f"```{root_pass}```",             False),
            ("SSH Command",     f"```{direct_ssh_cmd}```",        False),
        ]
        if exp_at: fields.append(("⏰ Expiry", exp_note, False))
        dm = await user.create_dm()
        await dm.send(embed=em(
            "⚡ Your VPS is Ready",
            "An admin deployed a VPS for you!\n"
            "⚠️ **Keep your root password private.**",
            GREEN, fields=fields,
        ))
        dm_ok = True
        log.info(f"[{vps_id}] DM sent to {user}")
    except discord.Forbidden:
        log.warning(f"[{vps_id}] Cannot DM {user} — DMs disabled")
    except Exception as e:
        log.error(f"[{vps_id}] DM send failed: {e}")

    # ── Public channel notification ───────────────────────────────────
    try:
        ch = ix.channel or bot.get_channel(ix.channel_id)
        if ch:
            await ch.send(embed=em(
                "✅ VPS Ready",
                f"{user.mention} your VPS **`{vps_id}`** is live!\n"
                f"📬 Check your **DMs** for SSH credentials.",
                GREEN,
                fields=[
                    ("Instance ID", f"`{vps_id}`",   True),
                    ("OS",          os_label,          True),
                    ("Node",        node_id or "N1",   True),
                ],
            ))
            log.info(f"[{vps_id}] Channel notification sent")
        else:
            log.warning(f"[{vps_id}] ix.channel is None — channel notification skipped")
    except Exception as e:
        log.error(f"[{vps_id}] Channel notification failed: {e}")

    # ── SSH port reachability check (quick, non-blocking) ─────────────
    port_open = False
    try:
        def _check_port(ip, port, timeout=5):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                return s.connect_ex((ip, port)) == 0
        loop = asyncio.get_running_loop()
        port_open = await loop.run_in_executor(None, lambda: _check_port(ssh_ip, host_port))
        log.info(f"[{vps_id}] SSH port {host_port} reachable: {port_open}")
    except Exception as e:
        log.warning(f"[{vps_id}] Port check failed: {e}")

    if not port_open:
        try:
            dm = await user.create_dm()
            await dm.send(embed=em(
                "⚠️ SSH Port May Be Blocked",
                f"Port `{host_port}` on `{ssh_ip}` is **not responding**.\n\n"
                f"Your VPS is running — ask your admin to open the firewall:\n"
                f"```bash\n"
                f"sudo ufw allow 20000:29999/tcp && sudo ufw reload\n"
                f"```\n"
                f"Also open `20000-29999` TCP in your cloud provider's Security Group.\n\n"
                f"Then retry:\n```\n{direct_ssh_cmd}\n```",
                YELLOW,
            ))
        except Exception:
            pass

    # ── Admin ephemeral followup ───────────────────────────────────────
    note = "✅ DM sent." if dm_ok else "⚠️ Could not DM user."
    if not port_open:
        note += " ❌ SSH port blocked — firewall needed."
    try:
        await ix.followup.send(embed=em(
            "✅ VPS Created",
            f"**{vps_id}** is live for {user.mention}\n{note}",
            GREEN if (dm_ok and port_open) else YELLOW,
            fields=[
                ("🆔 VPS ID",    vps_id,          True),
                ("👤 Owner",     str(user),        True),
                ("🖥 OS",        os_label,         True),
                ("🧠 RAM",       f"{ram} MB",      True),
                ("💻 CPU",       f"{cpu} Core(s)", True),
                ("🌐 SSH Port",  f"`{host_port}`", True),
                ("🔌 Port Open", "✅ Yes" if port_open else "❌ No — open firewall", True),
                ("⏰ Expiry",    exp_note,         False),
            ],
        ))
    except Exception as e:
        log.error(f"[{vps_id}] Final followup.send failed: {e}")
        # Fallback — try channel if followup expired
        try:
            ch = ix.channel or bot.get_channel(ix.channel_id)
            if ch:
                await ch.send(
                    content=f"{ix.user.mention} ✅ **{vps_id}** created for {user.mention}. "
                            f"SSH sent to their DM.",
                )
        except Exception:
            pass

# ─────────────────────────────────────────────────────
# BOT
# ─────────────────────────────────────────────────────
intents         = discord.Intents.default()
intents.members = True

class RivenCloudBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        log.info("Commands synced.")
        await start_agent_server()

    async def on_ready(self):
        log.info(f"Online as {self.user}")
        if not auto_suspend.is_running():
            auto_suspend.start()
        if not update_status.is_running():
            update_status.start()

bot = RivenCloudBot()

# ─────────────────────────────────────────────────────
# LIVE STATUS TASK — "RIVEN CLOUD | {n} VPS Running"
# ─────────────────────────────────────────────────────
@tasks.loop(minutes=2)
async def update_status():
    try:
        with get_db() as c:
            count = c.execute(
                "SELECT COUNT(*) AS n FROM vps WHERE status='running'"
            ).fetchone()["n"]
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"RIVEN CLOUD | {count} VPS Running"))
    except Exception as e:
        log.warning(f"Status update failed: {e}")

@update_status.before_loop
async def _status_before(): await bot.wait_until_ready()


async def perform_vps_action(row, action):
    backend = (row["backend"] if "backend" in row.keys() else "docker") or "docker"
    if row["node_id"]:
        result = await send_job_to_node(row["node_id"], {
            "type": "exec_action", "action": action, "vps_id": row["vps_id"], "backend": backend
        }, timeout=180)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Remote action failed"))
        return
    if backend == "incus":
        await asyncio.get_running_loop().run_in_executor(None, incus_action, row["vps_id"], action)
        return
    def run_docker():
        ct = get_docker().containers.get(row["container_id"] or row["vps_id"])
        if action == "start": ct.start()
        elif action == "stop": ct.stop()
        elif action == "restart": ct.restart()
        elif action == "remove": ct.remove(force=True, v=True)
        else: raise RuntimeError(f"Unknown action: {action}")
    await asyncio.get_running_loop().run_in_executor(None, run_docker)


# ─────────────────────────────────────────────────────
# AUTO-SUSPEND TASK
# ─────────────────────────────────────────────────────
@tasks.loop(minutes=15)
async def auto_suspend():
    now = datetime.datetime.utcnow()
    with get_db() as c:
        rows = c.execute(
            "SELECT * FROM vps WHERE expires_at IS NOT NULL AND status!='suspended'"
        ).fetchall()
    for row in rows:
        try:
            if now < datetime.datetime.fromisoformat(row["expires_at"]): continue
        except Exception: continue
        vid = row["vps_id"]
        log.info(f"[{vid}] Auto-suspending.")
        try: await perform_vps_action(row, "stop")
        except Exception: pass
        if PTERO_ON and row["ptero_id"]:
            try: ptero_suspend(row["ptero_id"])
            except Exception as e: log.warning(f"Ptero suspend: {e}")
        with get_db() as c:
            c.execute("UPDATE vps SET status='suspended' WHERE vps_id=?", (vid,))
        try:
            u = await bot.fetch_user(row["owner_id"])
            await u.send(embed=em("⏰ VPS Suspended",
                f"Your VPS **{vid}** has expired and been suspended.\nContact admin to reactivate.",
                YELLOW))
        except Exception: pass

@auto_suspend.before_loop
async def _before(): await bot.wait_until_ready()

# ══════════════════════════════════════════════
# USER COMMANDS
# ══════════════════════════════════════════════

@bot.tree.command(name="start", description="Start your VPS.")
@app_commands.describe(vps_id="e.g. rivencloud-vps-0001")
async def cmd_start(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    if row["status"] == "suspended":
        return await ix.followup.send(embed=em("⛔ Suspended", "Contact an admin to reactivate.", YELLOW))
    try:
        await perform_vps_action(row, "start")
        if PTERO_ON and row["ptero_id"]:
            try: ptero_unsuspend(row["ptero_id"])
            except Exception: pass
        with get_db() as c: c.execute("UPDATE vps SET status='running' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("✅ Started",
            f"**{vps_id}** is running.\nUse `/regen-ssh {vps_id}` for a fresh SSH link.", GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="stop", description="Stop your VPS.")
@app_commands.describe(vps_id="e.g. rivencloud-vps-0001")
async def cmd_stop(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT container_id FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try:
        await perform_vps_action(row, "stop")
        with get_db() as c: c.execute("UPDATE vps SET status='stopped' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("🛑 Stopped", f"**{vps_id}** stopped.", YELLOW))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="restart", description="Restart your VPS.")
@app_commands.describe(vps_id="e.g. rivencloud-vps-0001")
async def cmd_restart(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT container_id,status FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    if row["status"] == "suspended":
        return await ix.followup.send(embed=em("⛔ Suspended", "Contact an admin to reactivate.", YELLOW))
    try:
        await perform_vps_action(row, "restart")
        with get_db() as c: c.execute("UPDATE vps SET status='running' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("🔄 Restarted",
            f"**{vps_id}** restarted.\nUse `/regen-ssh {vps_id}` for a fresh SSH link.", GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="reinstall", description="Reinstall your VPS (same specs, data wiped).")
@app_commands.describe(vps_id="e.g. rivencloud-vps-0001")
async def cmd_reinstall(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    await ix.followup.send(embed=em("⏳ Reinstalling...", "~90 seconds...", YELLOW))
    try:
        try: await perform_vps_action(row, "remove")
        except Exception: pass

        # Reuse existing port; rotate the root password since data is wiped.
        host_port = row["ssh_port"] or find_free_port()
        root_pass = gen_root_password()

        ct, ssh = await asyncio.get_running_loop().run_in_executor(
            None, lambda: provision(vps_id, row["os_image"], row["os_label"],
                                    row["ram_mb"], row["cpu_cores"], row["disk_gb"], row["cpu_name"],
                                    host_port, root_pass)
        )
        with get_db() as c:
            c.execute("""UPDATE vps SET container_id=?,ssh_cmd=?,ssh_ip=?,ssh_port=?,
                         root_pass=?,status='running' WHERE vps_id=?""",
                      (ct.id, ssh, SERVER_IP, host_port, root_pass, vps_id))
        try:
            dm = await ix.user.create_dm()
            direct_ssh_cmd = f"ssh root@{SERVER_IP} -p {host_port}"
            await dm.send(embed=em("🔄 Reinstalled", f"**{vps_id}** rebuilt — data wiped.", GREEN,
                fields=[
                    ("Shared IPv4",    f"`{SERVER_IP}`",          True),
                    ("SSH Port (NAT)", f"`{host_port}`",          True),
                    ("Username",       "`root`",                  True),
                    ("Root Password",  f"```{root_pass}```",      False),
                    ("SSH Command",    f"```{direct_ssh_cmd}```", False),
                ]))
        except discord.Forbidden: pass
        await ix.followup.send(embed=em("✅ Reinstalled", f"**{vps_id}** done. Check DMs.", GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="port-add", description="Add a port forward for your VPS (max 10).")
@app_commands.describe(
    vps_id="Your VPS ID, e.g. rivencloud-vps-0001",
    host_port="Port on the public server (e.g. 30001)",
    container_port="Port inside your VPS to forward to (e.g. 80, 3000, 8080)",
)
async def cmd_port_add(ix: discord.Interaction, vps_id: str, host_port: int, container_port: int):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c:
        row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row:
        return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    if row["status"] != "running":
        return await ix.followup.send(embed=em("⚠️ Not Running", f"Start your VPS first: `/start {vps_id}`", YELLOW))
    if not (1 <= host_port <= 65535) or not (1 <= container_port <= 65535):
        return await ix.followup.send(embed=em("❌ Invalid Port", "Ports must be between 1 and 65535.", RED))
    if host_port in range(20000, 30000):
        return await ix.followup.send(embed=em("❌ Reserved Range", "Ports 20000-29999 are reserved for VPS SSH. Pick another port.", RED))
    try:
        container_ip = await asyncio.get_running_loop().run_in_executor(
            None, lambda: add_port_forward(vps_id, row["container_id"], host_port, container_port)
        )
        server_ip = row["ssh_ip"] or SERVER_IP
        await ix.followup.send(embed=em(
            "✅ Port Forward Added",
            f"Traffic to **`{server_ip}:{host_port}`** will now be forwarded to port **`{container_port}`** inside your VPS.",
            GREEN,
            fields=[
                ("VPS ID",          f"`{vps_id}`",          True),
                ("Host Port",       f"`{host_port}`",        True),
                ("Container Port",  f"`{container_port}`",   True),
                ("Connect Example", f"`{server_ip}:{host_port}`", False),
            ],
        ))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="port-list", description="List all port forwards for your VPS.")
@app_commands.describe(vps_id="Your VPS ID, e.g. rivencloud-vps-0001")
async def cmd_port_list(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id) and not is_admin(ix):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c:
        rows = c.execute(
            "SELECT * FROM port_forwards WHERE vps_id=? ORDER BY host_port", (vps_id,)
        ).fetchall()
        vps = c.execute("SELECT ssh_ip FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not rows:
        return await ix.followup.send(embed=em(
            "📋 No Port Forwards",
            f"**{vps_id}** has no port forwards yet.\nUse `/port-add {vps_id} <host_port> <container_port>` to add one.",
            BLUE,
        ))
    server_ip = (vps["ssh_ip"] if vps else None) or SERVER_IP
    fields = []
    for r in rows:
        fields.append((
            f"Host :{r['host_port']}",
            f"→ VPS :{r['container_port']} | Connect: `{server_ip}:{r['host_port']}`",
            False,
        ))
    await ix.followup.send(embed=em(
        f"📋 Port Forwards — {vps_id} ({len(rows)}/{PORT_FWD_MAX})",
        "", BLUE, fields=fields,
    ))


@bot.tree.command(name="port-remove", description="Remove a port forward from your VPS.")
@app_commands.describe(
    vps_id="Your VPS ID, e.g. rivencloud-vps-0001",
    host_port="The host port to remove (from /port-list)",
)
async def cmd_port_remove(ix: discord.Interaction, vps_id: str, host_port: int):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id) and not is_admin(ix):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c:
        row = c.execute("SELECT container_id FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row:
        return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: remove_port_forward(vps_id, row["container_id"], host_port)
        )
        await ix.followup.send(embed=em(
            "🗑 Port Forward Removed",
            f"Host port **`{host_port}`** removed from **{vps_id}**.",
            YELLOW,
        ))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="regen-ssh", description="Get a fresh tmate SSH session.")
@app_commands.describe(vps_id="e.g. rivencloud-vps-0001")
async def cmd_regen(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    if row["status"] != "running":
        return await ix.followup.send(embed=em("⚠️ Not Running", f"Start first: `/start {vps_id}`", YELLOW))
    try:
        backend = (row["backend"] if "backend" in row.keys() else "docker") or "docker"
        if backend == "incus":
            ssh = await asyncio.get_running_loop().run_in_executor(None, incus_regen_ssh, vps_id)
        else:
            ct = get_docker().containers.get(row["container_id"])
            ssh = await asyncio.get_running_loop().run_in_executor(None, lambda: regen_ssh(ct))
        if not ssh:
            return await ix.followup.send(embed=em("⚠️ Not Ready", "Try again in 15 seconds.", YELLOW))
        with get_db() as c: c.execute("UPDATE vps SET ssh_cmd=? WHERE vps_id=?", (ssh, vps_id))
        await ix.followup.send(embed=em(f"🔑 SSH Session — {vps_id}",
            "⚠️ Keep private — anyone with this can access your terminal.",
            GREEN, fields=[("🖥 SSH Command", f"```{ssh}```", False)]))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="vps-performance", description="Live stats for your VPS.")
@app_commands.describe(vps_id="e.g. rivencloud-vps-0001")
async def cmd_perf(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try:
        backend = (row["backend"] if "backend" in row.keys() else "docker") or "docker"
        if backend == "incus":
            details = await asyncio.get_running_loop().run_in_executor(
                None, lambda: incus_run("info", vps_id).stdout)
            disk_line = await asyncio.get_running_loop().run_in_executor(
                None, lambda: incus_run("exec", vps_id, "--", "bash", "-lc", "df -h / | tail -1").stdout.strip())
            return await ix.followup.send(embed=em("📊 VPS Performance",
                f"**Runtime:** Incus LXC\n**Disk:** `{disk_line or 'N/A'}`\n"
                f"**Incus info:**\n```{details[-800:]}```", BLUE))
        ct = get_docker().containers.get(row["container_id"])
        ct.reload()
        if ct.status != "running":
            return await ix.followup.send(embed=em("⚠️ Not Running", f"Start first: `/start {vps_id}`", YELLOW))
        s  = get_stats(ct, row["ram_mb"], row["cpu_cores"])
        dr = ct.exec_run("df -BM / --output=used | tail -1", tty=False)
        du = "N/A"
        if dr.exit_code == 0:
            raw = dr.output.decode().strip().replace("M","").strip()
            try: du = f"{round(int(raw)/1024,2)} GB"
            except Exception: du = raw + " MB"
        pf = [("🦅 Ptero ID", str(row["ptero_id"]), True)] if PTERO_ON and row["ptero_id"] else []
        await ix.followup.send(embed=em("📊 VPS Performance", "", BLUE, fields=[
            ("🆔 VPS ID",    vps_id,                                               True),
            ("🖥 OS",        row["os_label"] or row["os_image"],                   True),
            ("🏷 CPU Model", row["cpu_name"],                                       True),
            ("💻 CPU",       f"{s['cpu']}% of {row['cpu_cores']} Core(s)",        True),
            ("🧠 RAM",       f"{s['mem_mb']} MB / {row['ram_mb']} MB ({s['mem_p']}%)", True),
            ("💾 Disk",      f"{du} / {row['disk_gb']} GB",                        True),
            ("⏱ Uptime",    s["up"],                                               True),
            ("🌐 Net RX",    f"{s['rx']} MB",                                      True),
            ("🌐 Net TX",    f"{s['tx']} MB",                                      True),
            *pf,
        ]))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="my-vps", description="List all your VPS instances.")
async def cmd_my_vps(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    with get_db() as c:
        rows = c.execute("SELECT * FROM vps WHERE owner_id=? ORDER BY vps_id", (ix.user.id,)).fetchall()
    if not rows:
        return await ix.followup.send(embed=em("📋 My VPS", "You have no VPS instances.", YELLOW))
    fields = []
    for r in rows:
        line = (f"OS:`{r['os_label']}` RAM:`{r['ram_mb']}MB` "
                f"CPU:`{r['cpu_cores']}` Disk:`{r['disk_gb']}GB` Status:`{r['status']}`")
        if r["expires_at"]:
            try:
                ts = int(datetime.datetime.fromisoformat(r["expires_at"]).timestamp())
                line += f"\n⏰ Expires: <t:{ts}:R>"
            except Exception: pass
        fields.append((r["vps_id"], line, False))
    await ix.followup.send(embed=em(f"📋 My VPS ({len(rows)})", "", BLUE, fields=fields))


@bot.tree.command(name="commands", description="Show all commands.")
async def cmd_commands(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    u = em("👤 User Commands", "", BLUE, fields=[
        ("`/start <id>`",           "▶️  Start VPS",                      False),
        ("`/stop <id>`",            "⏹️  Stop VPS",                       False),
        ("`/restart <id>`",         "🔄  Restart VPS",                    False),
        ("`/reinstall <id>`",       "🔁  Wipe & reinstall",               False),
        ("`/regen-ssh <id>`",       "🔑  Fresh tmate SSH session",        False),
        ("`/vps-performance <id>`", "📊  Live CPU/RAM/Disk/Net stats",    False),
        ("`/my-vps`",               "📋  List your VPS instances",        False),
        ("`/redeem <code>`",       "🎟️  Redeem a VPS code",              False),
        ("`/commands`",             "📖  This help",                      False),
    ])
    a = em("🛡️ Admin Commands", "", RED, fields=[
        ("`/deploy <user>`",                                     "🎛️  1-click deploy",          False),
        ("`/create <user> <ram> <cpu> <disk> <os> <cpu> <days>`","➕  Full param create",       False),
        ("`/admin-add-user <user>`",                             "✅  Grant access",            False),
        ("`/admin-remove-user <user>`",                          "❌  Revoke access",           False),
        ("`/extend-vps <id> <days>`",                            "⏰  Extend/remove expiry",   False),
        ("`/suspend-vps <id>`",                                  "⛔  Suspend VPS",             False),
        ("`/unsuspend-vps <id>`",                                "🔓  Unsuspend VPS",          False),
        ("`/remove-vps <id>`",                                   "🗑️  Delete VPS",             False),
        ("`/fix-vps <id>`",                                      "🔧  Remove stuck container", False),
        ("`/list-vps`",                                          "📋  List all VPS",           False),
        ("`/node-stats`",                                        "🖥️  Host stats",             False),
        ("`/check-network`",                                     "🌐  Diagnose SERVER_IP/ports", False),
        ("`/gen-redeem <ram> <cpu> <disk> <days> <count>`",      "🎟️  Generate redeem code(s)", False),
        ("`/redeem-stock`",                                      "📦  View unredeemed codes",  False),
        ("`/node-create <name>`",                                "📡  Register a new node",    False),
        ("`/node-config <name>`",                                "🔗  Get node install/connect cmd", False),
        ("`/node-list`",                                         "📋  List nodes + online status", False),
        ("`/node-delete <name>`",                                "🗑️  Delete a node",          False),
        ("`/ptero-status`",                                      "🦅  Pterodactyl status",     False),
    ])
    r = em("📖 Reference", "", DARK, fields=[
        ("VPS ID",      "`rivencloud-vps-0001`, `rivencloud-vps-0002` ...",                 False),
        ("OS",          "`ubuntu20` `ubuntu22` `ubuntu24` `debian11` `debian12`",        False),
        ("CPU",         "`ryzen9` → AMD Ryzen 9 9950X\n`xeon` → Intel Xeon Platinum 8480+", False),
        ("SSH Access",  "tmate SSH only — sent to DM, never public",                     False),
        ("systemctl",   "Full systemd — `systemctl`, services, cron all work",           False),
        ("Pterodactyl", "Wings manages Docker workloads only; Incus LXC is managed by Incus.", False),
    ])
    await ix.followup.send(embeds=[u, a, r])

# ══════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════

@bot.tree.command(name="admin-add-user", description="[Admin] Grant hosting access.")
@app_commands.describe(user="User to grant access")
async def cmd_add(ix: discord.Interaction, user: discord.Member):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    with get_db() as c:
        c.execute("INSERT OR IGNORE INTO allowed_users (user_id,added_by) VALUES (?,?)", (user.id, ix.user.id))
    await ix.followup.send(embed=em("✅ Added", f"{user.mention} granted access.", GREEN))


@bot.tree.command(name="admin-remove-user", description="[Admin] Revoke hosting access.")
@app_commands.describe(user="User to revoke")
async def cmd_rm(ix: discord.Interaction, user: discord.Member):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    with get_db() as c: c.execute("DELETE FROM allowed_users WHERE user_id=?", (user.id,))
    await ix.followup.send(embed=em("🗑 Removed", f"{user.mention} access revoked.", YELLOW))


async def node_autocomplete(ix: discord.Interaction, current: str):
    with get_db() as c:
        rows = c.execute("SELECT node_id, status FROM nodes").fetchall()
    choices = [app_commands.Choice(name="🟢 N1 (This Server)", value="local")]
    for r in rows:
        label = f"{r['node_id']} ({'🟢 online' if r['status']=='online' else '🔴 offline'})"
        choices.append(app_commands.Choice(name=label, value=r["node_id"]))
    if current:
        choices = [ch for ch in choices if current.lower() in ch.name.lower()]
    return choices[:25]

async def existing_node_autocomplete(ix: discord.Interaction, current: str):
    with get_db() as c:
        rows = c.execute("SELECT node_id, status FROM nodes").fetchall()
    choices = [
        app_commands.Choice(name=f"{r['node_id']} ({'🟢 online' if r['status']=='online' else '🔴 offline'})",
                             value=r["node_id"])
        for r in rows
    ]
    if current:
        choices = [ch for ch in choices if current.lower() in ch.name.lower()]
    return choices[:25]


@bot.tree.command(name="create", description="[Admin] Create VPS with full parameters.")
@app_commands.describe(user="Target user", ram="RAM in MB", cpu="CPU cores",
    disk="Disk in GB", os="OS", cpu_name="CPU model", suspend_in_days="Days until auto-suspend (0=never)",
    node="Which node to deploy on (leave blank for local)", backend="docker or incus")
@app_commands.choices(
    os=[
        app_commands.Choice(name="Ubuntu 20.04", value="ubuntu20"),
        app_commands.Choice(name="Ubuntu 22.04", value="ubuntu22"),
        app_commands.Choice(name="Ubuntu 24.04", value="ubuntu24"),
        app_commands.Choice(name="Debian 11",    value="debian11"),
        app_commands.Choice(name="Debian 12",    value="debian12"),
    ],
    cpu_name=[
        app_commands.Choice(name="AMD Ryzen 9 9950X",         value="ryzen9"),
        app_commands.Choice(name="Intel Xeon Platinum 8480+", value="xeon"),
    ],
)
@app_commands.autocomplete(node=node_autocomplete)
async def cmd_create(ix: discord.Interaction, user: discord.Member, ram: int, cpu: float,
    disk: int, os: app_commands.Choice[str], cpu_name: app_commands.Choice[str],
    suspend_in_days: int = 0, node: str = "local", backend: str = "docker"):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    target_node = None if not node or node == "local" else node
    await do_create(ix, user, ram, cpu, disk, os.value, cpu_name.value, suspend_in_days, target_node, backend)


@bot.tree.command(name="extend-vps", description="[Admin] Extend or remove expiry.")
@app_commands.describe(vps_id="VPS ID", days="Days from now (0 = never)")
async def cmd_extend(ix: discord.Interaction, vps_id: str, days: int):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    vps_id = vps_id.lower()
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    if days <= 0:
        with get_db() as c: c.execute("UPDATE vps SET expires_at=NULL WHERE vps_id=?", (vps_id,))
        return await ix.followup.send(embed=em("✅ Expiry Removed", f"**{vps_id}** never auto-suspends.", GREEN))
    dt = datetime.datetime.utcnow() + datetime.timedelta(days=days)
    with get_db() as c: c.execute("UPDATE vps SET expires_at=? WHERE vps_id=?", (dt.isoformat(), vps_id))
    ts = int(dt.timestamp())
    await ix.followup.send(embed=em("✅ Expiry Set", f"**{vps_id}** auto-suspends <t:{ts}:R>.", GREEN))
    try:
        u = await bot.fetch_user(row["owner_id"])
        await u.send(embed=em("⏰ Expiry Updated", f"Your VPS **{vps_id}** auto-suspends <t:{ts}:R>.", BLUE))
    except Exception: pass


@bot.tree.command(name="suspend-vps", description="[Admin] Suspend a VPS.")
@app_commands.describe(vps_id="VPS ID")
async def cmd_suspend(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    vps_id = vps_id.lower()
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try: await perform_vps_action(row, "stop")
    except Exception: pass
    if PTERO_ON and row["ptero_id"]:
        try: ptero_suspend(row["ptero_id"])
        except Exception as e: log.warning(f"Ptero suspend: {e}")
    with get_db() as c: c.execute("UPDATE vps SET status='suspended' WHERE vps_id=?", (vps_id,))
    await ix.followup.send(embed=em("⛔ Suspended", f"**{vps_id}** suspended.", YELLOW))


@bot.tree.command(name="unsuspend-vps", description="[Admin] Reactivate a VPS.")
@app_commands.describe(vps_id="VPS ID")
async def cmd_unsuspend(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    vps_id = vps_id.lower()
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try:
        await perform_vps_action(row, "start")
        if PTERO_ON and row["ptero_id"]:
            try: ptero_unsuspend(row["ptero_id"])
            except Exception as e: log.warning(f"Ptero unsuspend: {e}")
        with get_db() as c: c.execute("UPDATE vps SET status='running' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("✅ Unsuspended",
            f"**{vps_id}** is active. User can run `/regen-ssh {vps_id}`.", GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="remove-vps", description="[Admin] Permanently delete a VPS.")
@app_commands.describe(vps_id="VPS ID")
async def cmd_remove(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    vps_id = vps_id.lower()
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try: await perform_vps_action(row, "remove")
    except Exception: pass
    if PTERO_ON and row["ptero_id"]:
        try: ptero_remove(row["ptero_id"])
        except Exception as e: log.warning(f"Ptero delete: {e}")
    with get_db() as c: c.execute("DELETE FROM vps WHERE vps_id=?", (vps_id,))
    await ix.followup.send(embed=em("🗑 Deleted", f"**{vps_id}** permanently deleted.", YELLOW))


@bot.tree.command(name="fix-vps", description="[Admin] Force-remove a stuck container.")
@app_commands.describe(vps_id="VPS ID to fix")
async def cmd_fix(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    vps_id  = vps_id.lower()
    removed = False
    try:
        with get_db() as c: fixrow = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
        if fixrow and (fixrow["backend"] or "docker") == "incus":
            await asyncio.get_running_loop().run_in_executor(None, incus_action, vps_id, "remove")
        else:
            get_docker().containers.get(vps_id).remove(force=True)
        removed = True
        log.info(f"{ix.user} fixed stuck container {vps_id}")
    except docker.errors.NotFound: pass
    except Exception as e:
        return await ix.followup.send(embed=em("❌ Error", str(e), RED))
    with get_db() as c:
        if c.execute("SELECT 1 FROM vps WHERE vps_id=?", (vps_id,)).fetchone():
            c.execute("UPDATE vps SET status='stopped' WHERE vps_id=?", (vps_id,))
    msg = (f"Removed stuck container for **{vps_id}**.\nNow run `/reinstall {vps_id}` or `/create` again."
           if removed else f"No stuck container found for **{vps_id}** — already clean.")
    await ix.followup.send(embed=em("✅ Fixed" if removed else "ℹ️ Clean", msg,
                                    GREEN if removed else BLUE))


@bot.tree.command(name="list-vps", description="[Admin] List all VPS on the node.")
async def cmd_list(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    with get_db() as c: rows = c.execute("SELECT * FROM vps ORDER BY vps_id").fetchall()
    if not rows: return await ix.followup.send(embed=em("📋 All VPS", "None found.", YELLOW))
    fields = []
    for r in rows:
        line = (f"<@{r['owner_id']}> OS:`{r['os_label']}` RAM:`{r['ram_mb']}MB` "
                f"CPU:`{r['cpu_cores']}` Disk:`{r['disk_gb']}GB` Status:`{r['status']}`")
        if PTERO_ON and r["ptero_id"]: line += f" 🦅`{r['ptero_id']}`"
        if r["expires_at"]:
            try:
                ts = int(datetime.datetime.fromisoformat(r["expires_at"]).timestamp())
                line += f" Expires:<t:{ts}:R>"
            except Exception: pass
        fields.append((r["vps_id"], line, False))
    for i in range(0, len(fields), 25):
        await ix.followup.send(embed=em(f"📋 All VPS ({len(rows)}) — Page {i//25+1}", "", BLUE,
                                        fields=fields[i:i+25]))


@bot.tree.command(name="node-stats", description="[Admin] Host node resource usage.")
async def cmd_node(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    dsk = psutil.disk_usage("/")
    try:
        cl = get_docker()
        running = len([c for c in cl.containers.list() if c.status == "running"])
        total   = len(cl.containers.list(all=True))
    except Exception: running = total = 0
    pf = []
    if PTERO_ON:
        s  = ptero_check()
        pf = [("🦅 Pterodactyl",
               f"✅ {s.get('nodes',0)} node(s)" if s["ok"] else f"❌ {s.get('error','Error')}", False)]
    await ix.followup.send(embed=em("🖥️ Node Stats", "", BLUE, fields=[
        ("🖥 Host CPU",    f"{cpu}%",                                                                        True),
        ("🧠 Host RAM",    f"{round(mem.used/1024**3,2)}/{round(mem.total/1024**3,2)} GB ({mem.percent}%)", True),
        ("💾 Host Disk",   f"{gb(dsk.used)}/{gb(dsk.total)} GB ({dsk.percent}%)",                           True),
        ("🐳 Running",     str(running),                                                                     True),
        ("📦 Total",       str(total),                                                                       True),
        *pf,
    ]))


@bot.tree.command(name="check-network", description="[Admin] Diagnose SERVER_IP + SSH port setup.")
async def cmd_check_network(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))

    lines  = []
    ok_all = True

    # 1. Docker reachable?
    try:
        get_docker()
        lines.append("✅ Docker socket reachable")
    except Exception as e:
        ok_all = False
        lines.append(f"❌ Docker socket NOT reachable — {str(e)[:150]}")

    # 2. Does SERVER_IP match this machine's real public IP?
    real_ip = None
    try:
        real_ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
        if real_ip == SERVER_IP:
            lines.append(f"✅ SERVER_IP matches this machine's public IP (`{SERVER_IP}`)")
        else:
            ok_all = False
            lines.append(
                f"❌ SERVER_IP (`{SERVER_IP}`) does NOT match this machine's "
                f"actual public IP (`{real_ip}`) — update your `.env`"
            )
    except Exception as e:
        lines.append(f"⚠️ Could not verify public IP (no internet from this host?) — {str(e)[:120]}")

    # 3. Is the SSH port range free to bind locally?
    sample_port = SSH_PORT_START
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", sample_port))
        lines.append(f"✅ Port `{sample_port}` is bindable locally (range looks usable)")
    except Exception as e:
        ok_all = False
        lines.append(f"❌ Port `{sample_port}` failed to bind locally — {str(e)[:120]}")

    lines.append(
        "\n⚠️ This command can only check the **local** machine. It "
        "**cannot** confirm your cloud firewall / security group allows "
        f"inbound TCP on `{SSH_PORT_START}-{SSH_PORT_END}` — verify that "
        "separately in your provider's dashboard, then test from another "
        "machine with:\n```nc -zv " + (real_ip or SERVER_IP) + f" {sample_port}```"
    )

    await ix.followup.send(embed=em(
        "✅ Network Check Passed" if ok_all else "⚠️ Network Check Found Issues",
        "\n".join(lines),
        GREEN if ok_all else YELLOW,
    ))


# ══════════════════════════════════════════════
# REDEEM CODE SYSTEM
# ══════════════════════════════════════════════
@bot.tree.command(name="gen-redeem", description="[Admin] Generate VPS redeem code(s).")
@app_commands.describe(
    ram="RAM in MB for the VPS this code grants",
    cpu="CPU cores for the VPS this code grants",
    disk="Disk in GB for the VPS this code grants",
    valid_days="Auto-suspend after this many days once redeemed (0=never)",
    count="How many codes to generate at once (max 25)",
)
async def cmd_gen_redeem(ix: discord.Interaction, ram: int, cpu: float, disk: int,
                          valid_days: int = 0, count: int = 1):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    if count < 1 or count > 25:
        return await ix.followup.send(embed=em("❌ Invalid", "`count` must be between 1 and 25.", RED))

    codes = []
    with get_db() as c:
        for _ in range(count):
            code = gen_redeem_code()
            c.execute(
                "INSERT INTO redeem_codes (code,ram_mb,cpu_cores,disk_gb,valid_days,created_by) VALUES (?,?,?,?,?,?)",
                (code, ram, cpu, disk, valid_days, ix.user.id),
            )
            codes.append(code)

    block = "\n".join(codes)
    await ix.followup.send(embed=em(
        f"🎟️ {count} Redeem Code(s) Generated",
        f"Each code grants: **{ram}MB RAM / {cpu} vCPU / {disk}GB Disk** "
        f"({'never expires' if valid_days == 0 else f'{valid_days}-day auto-suspend'})\n"
        f"```\n{block}\n```\n"
        "Give these to members — each works **once**, with `/redeem <code>`.",
        GREEN,
    ))


@bot.tree.command(name="redeem", description="Redeem a VPS code.")
@app_commands.describe(code="Your redeem code")
async def cmd_redeem(ix: discord.Interaction, code: str):
    await ix.response.defer(ephemeral=True)
    code = code.strip().upper()

    # Atomic claim: DELETE ... RETURNING guarantees only one caller ever
    # gets the row back, even if two people redeem the same code at once.
    with get_db() as c:
        row = c.execute(
            "DELETE FROM redeem_codes WHERE code=? RETURNING ram_mb, cpu_cores, disk_gb, valid_days",
            (code,),
        ).fetchone()

    if not row:
        return await ix.followup.send(embed=em(
            "❌ Invalid Code",
            "This code doesn't exist or has already been redeemed.", RED))

    await do_create(
        ix, ix.user, row["ram_mb"], row["cpu_cores"], row["disk_gb"],
        "ubuntu24", "ryzen9", row["valid_days"], None,
    )


@bot.tree.command(name="redeem-stock", description="[Admin] View unredeemed codes.")
async def cmd_redeem_stock(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    with get_db() as c:
        rows = c.execute("SELECT * FROM redeem_codes ORDER BY created_at DESC").fetchall()
    if not rows:
        return await ix.followup.send(embed=em("🎟️ Redeem Stock", "No unredeemed codes right now.", BLUE))
    lines = []
    for r in rows:
        exp = "never expires" if r["valid_days"] == 0 else f"{r['valid_days']}d"
        lines.append(f"`{r['code']}` → {r['ram_mb']}MB/{r['cpu_cores']}vCPU/{r['disk_gb']}GB ({exp})")
    text = "\n".join(lines[:40])
    if len(rows) > 40:
        text += f"\n… and {len(rows)-40} more."
    await ix.followup.send(embed=em(f"🎟️ Redeem Stock ({len(rows)})", text, BLUE))


# ══════════════════════════════════════════════
# NODE MANAGEMENT
# ══════════════════════════════════════════════
@bot.tree.command(name="node-create", description="[Admin] Register a new node.")
@app_commands.describe(name="Unique name for this node, e.g. Node1")
async def cmd_node_create(ix: discord.Interaction, name: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    name = name.strip()
    if not name or "|" in name:
        return await ix.followup.send(embed=em("❌ Invalid", "Name can't be empty or contain `|`.", RED))
    token = secrets.token_urlsafe(24)
    try:
        with get_db() as c:
            c.execute("INSERT INTO nodes (node_id, token, created_by) VALUES (?,?,?)",
                      (name, token, ix.user.id))
    except sqlite3.IntegrityError:
        return await ix.followup.send(embed=em("❌ Already Exists", f"A node named **{name}** already exists.", RED))
    await ix.followup.send(embed=em(
        "✅ Node Registered",
        f"Node **{name}** created (currently 🔴 offline).\n"
        f"Run `/node-config name:{name}` to get its install + connect command.",
        GREEN,
    ))


@bot.tree.command(name="node-config", description="[Admin] Get the install/connect command for a node.")
@app_commands.describe(name="Node to configure")
@app_commands.autocomplete(name=existing_node_autocomplete)
async def cmd_node_config(ix: discord.Interaction, name: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    with get_db() as c:
        row = c.execute("SELECT * FROM nodes WHERE node_id=?", (name,)).fetchone()
    if not row:
        return await ix.followup.send(embed=em("❌ Not Found", f"No node named **{name}**.", RED))

    install_cmd = (
        "curl -o node_agent.py "
        "https://raw.githubusercontent.com/atifqmi-max/vpsbot-v4/main/node_agent.py "
        "&& python3 node_agent.py"
    )
    connect_str = f"{row['node_id']}|{row['token']}|{SERVER_IP}|{AGENT_PORT}"

    await ix.followup.send(embed=em(
        f"🔗 Connect Node — {name}",
        "**Step 1.** On the new server, run this to install the agent:\n"
        f"```bash\n{install_cmd}\n```\n"
        "**Step 2.** From the menu that appears, choose **1) Install VPS Bot** "
        "first (one-time, sets up Docker).\n\n"
        "**Step 3.** Run the script again, choose **3) Connect NODE**, and paste this "
        "when it asks for the connect string:\n"
        f"```\n{connect_str}\n```\n"
        "As soon as it connects you'll see it go 🟢 online in `/node-list`.",
        BLUE,
    ))


@bot.tree.command(name="node-list", description="[Admin] List all nodes and their status.")
async def cmd_node_list(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    with get_db() as c:
        rows = c.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()
        vps_counts = {r["node_id"]: r["n"] for r in c.execute(
            "SELECT node_id, COUNT(*) AS n FROM vps WHERE node_id IS NOT NULL GROUP BY node_id"
        ).fetchall()}
        local_count = c.execute(
            "SELECT COUNT(*) AS n FROM vps WHERE node_id IS NULL"
        ).fetchone()["n"]

    lines = [f"🟢 **N1 (This Server)** — {local_count} VPS — `{SERVER_IP}`"]
    for r in rows:
        dot = "🟢 online" if node_is_online(r["node_id"]) else "🔴 offline"
        lines.append(
            f"**{r['node_id']}** — {dot} — {vps_counts.get(r['node_id'], 0)} VPS"
            + (f" — `{r['public_ip']}`" if r["public_ip"] else "")
        )
    await ix.followup.send(embed=em("📡 Nodes", "\n".join(lines), BLUE))


@bot.tree.command(name="node-delete", description="[Admin] Delete a node.")
@app_commands.describe(name="Node to delete")
@app_commands.autocomplete(name=existing_node_autocomplete)
async def cmd_node_delete(ix: discord.Interaction, name: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    with get_db() as c:
        row = c.execute("SELECT * FROM nodes WHERE node_id=?", (name,)).fetchone()
        if not row:
            return await ix.followup.send(embed=em("❌ Not Found", f"No node named **{name}**.", RED))
        still_used = c.execute(
            "SELECT COUNT(*) AS n FROM vps WHERE node_id=? AND status!='deleted'", (name,)
        ).fetchone()["n"]
        if still_used:
            return await ix.followup.send(embed=em(
                "❌ Node In Use",
                f"**{name}** still has {still_used} VPS on it. Remove or migrate them first "
                f"with `/remove-vps`.", RED))
        c.execute("DELETE FROM nodes WHERE node_id=?", (name,))
    ws = NODE_CONNECTIONS.pop(name, None)
    if ws:
        try: await ws.close()
        except Exception: pass
    await ix.followup.send(embed=em("🗑 Node Deleted", f"**{name}** has been removed.", YELLOW))


@bot.tree.command(name="ptero-status", description="[Admin] Pterodactyl panel status.")
async def cmd_ptero(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    if not PTERO_ON:
        return await ix.followup.send(embed=em("🦅 Not Configured",
            "Add `PTERO_URL` and `PTERO_API_KEY` to .env", YELLOW))
    s = ptero_check()
    if s["ok"]:
        try:
            nodes = ptero_get("nodes")
            nl = "\n".join(
                f"• **{n['attributes']['name']}** — `{n['attributes']['fqdn']}`"
                for n in nodes.get("data", [])
            ) or "No nodes."
        except Exception: nl = "Could not fetch nodes."
        await ix.followup.send(embed=em("🦅 Pterodactyl — Connected",
            f"Panel: `{PTERO_URL}`", GREEN, fields=[("Nodes", nl, False)]))
    else:
        await ix.followup.send(embed=em("🦅 Pterodactyl — Error",
            f"Panel: `{PTERO_URL}`\n```{s.get('error','Unknown')}```", RED))

# ══════════════════════════════════════════════
# 1-CLICK DEPLOY
# ══════════════════════════════════════════════

class DeployModal(discord.ui.Modal, title="🐉 RIVEN CLOUD — Deploy VPS"):
    ram  = discord.ui.TextInput(label="RAM (MB)",  placeholder="512",  default="512", min_length=1, max_length=7)
    cpu  = discord.ui.TextInput(label="CPU Cores", placeholder="1",    default="1",   min_length=1, max_length=5)
    disk = discord.ui.TextInput(label="Disk (GB)", placeholder="10",   default="10",  min_length=1, max_length=5)
    days = discord.ui.TextInput(label="Auto-Suspend After Days (0=never)", placeholder="0", default="0", min_length=1, max_length=4)
    backend = discord.ui.TextInput(label="Runtime: docker or incus", placeholder="docker / incus", default="docker", min_length=4, max_length=6)

    def __init__(self, target: discord.Member, os_key: str, cpu_key: str, node_id: str = None):
        super().__init__()
        self.target  = target
        self.os_key  = os_key
        self.cpu_key = cpu_key
        self.node_id = node_id

    async def on_submit(self, ix: discord.Interaction):
        await ix.response.defer(ephemeral=True)
        try:
            ram  = int(self.ram.value.strip())
            cpu  = float(self.cpu.value.strip())
            disk = int(self.disk.value.strip())
            days = int(self.days.value.strip())
            backend = self.backend.value.strip().lower()
            if backend not in ('docker', 'incus'): raise ValueError('Runtime must be docker or incus')
        except ValueError:
            return await ix.followup.send(embed=em("❌ Invalid", "All fields must be numbers.", RED))
        await do_create(ix, self.target, ram, cpu, disk, self.os_key, self.cpu_key, days, self.node_id, backend)


class OSView(discord.ui.View):
    def __init__(self, target: discord.Member):
        super().__init__(timeout=120)
        self.target = target

    async def pick(self, ix: discord.Interaction, key: str):
        if not is_admin(ix):
            return await ix.response.send_message(embed=em("⛔ Forbidden", "Admin only.", RED), ephemeral=True)
        _, label = OS_MAP[key]
        await ix.response.edit_message(
            embed=em("🐉 Deploy — Step 2/4", f"**OS:** {label}\n\nChoose **CPU**:", BLUE),
            view=CPUView(self.target, key),
        )

    @discord.ui.button(label="Ubuntu 20.04", style=discord.ButtonStyle.secondary, emoji="🐧", row=0)
    async def u20(self, ix, b): await self.pick(ix, "ubuntu20")
    @discord.ui.button(label="Ubuntu 22.04", style=discord.ButtonStyle.secondary, emoji="🐧", row=0)
    async def u22(self, ix, b): await self.pick(ix, "ubuntu22")
    @discord.ui.button(label="Ubuntu 24.04", style=discord.ButtonStyle.primary,   emoji="🐧", row=0)
    async def u24(self, ix, b): await self.pick(ix, "ubuntu24")
    @discord.ui.button(label="Debian 11",    style=discord.ButtonStyle.secondary, emoji="🌀", row=1)
    async def d11(self, ix, b): await self.pick(ix, "debian11")
    @discord.ui.button(label="Debian 12",    style=discord.ButtonStyle.primary,   emoji="🌀", row=1)
    async def d12(self, ix, b): await self.pick(ix, "debian12")
    @discord.ui.button(label="Cancel",       style=discord.ButtonStyle.danger,    emoji="✖️", row=1)
    async def cancel(self, ix: discord.Interaction, b):
        await ix.response.edit_message(embed=em("❌ Cancelled", "Deployment cancelled.", RED), view=None)


class CPUView(discord.ui.View):
    def __init__(self, target: discord.Member, os_key: str):
        super().__init__(timeout=120)
        self.target = target
        self.os_key = os_key

    async def pick(self, ix: discord.Interaction, key: str):
        if not is_admin(ix):
            return await ix.response.send_message(embed=em("⛔ Forbidden", "Admin only.", RED), ephemeral=True)
        await ix.response.edit_message(
            embed=em("🐉 Deploy — Step 3/4", "Choose which **node** to deploy this VPS on:", BLUE),
            view=NodeView(self.target, self.os_key, key),
        )

    @discord.ui.button(label="AMD Ryzen 9 9950X",        style=discord.ButtonStyle.danger,   emoji="🔴", row=0)
    async def ryzen(self, ix, b): await self.pick(ix, "ryzen9")
    @discord.ui.button(label="Intel Xeon Platinum 8480+", style=discord.ButtonStyle.primary,  emoji="🔵", row=0)
    async def xeon(self, ix, b):  await self.pick(ix, "xeon")
    @discord.ui.button(label="◀ Back",  style=discord.ButtonStyle.secondary, row=1)
    async def back(self, ix: discord.Interaction, b):
        await ix.response.edit_message(
            embed=em("🐉 Deploy — Step 1/4",
                     f"Deploying for **{self.target.display_name}**\n\nChoose **OS**:", BLUE),
            view=OSView(self.target),
        )
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️", row=1)
    async def cancel(self, ix: discord.Interaction, b):
        await ix.response.edit_message(embed=em("❌ Cancelled", "Deployment cancelled.", RED), view=None)


class NodeView(discord.ui.View):
    """Buttons for node selection — one button per node + N1 (local)."""

    def __init__(self, target: discord.Member, os_key: str, cpu_key: str):
        super().__init__(timeout=120)
        self.target  = target
        self.os_key  = os_key
        self.cpu_key = cpu_key
        self._build_buttons()

    def _build_buttons(self):
        # Always add N1 (local server) first
        self._add_node_button("N1 (This Server)", "local", online=True, row=0)

        with get_db() as c:
            rows = c.execute("SELECT node_id, status FROM nodes ORDER BY node_id").fetchall()

        row_index = 0
        for i, r in enumerate(rows):
            if i > 0 and i % 4 == 0:       # max 4 remote nodes per row
                row_index += 1
            if row_index > 2:               # Discord max 5 rows; keep last 2 for Back/Cancel
                break
            online = node_is_online(r["node_id"])
            self._add_node_button(r["node_id"], r["node_id"], online=online, row=row_index)

        # Back & Cancel always on last row
        back_btn   = discord.ui.Button(label="◀ Back",  style=discord.ButtonStyle.secondary, row=3)
        cancel_btn = discord.ui.Button(label="Cancel",  style=discord.ButtonStyle.danger,    row=3, emoji="✖️")
        back_btn.callback   = self._back
        cancel_btn.callback = self._cancel
        self.add_item(back_btn)
        self.add_item(cancel_btn)

    def _add_node_button(self, label: str, value: str, online: bool, row: int):
        style = discord.ButtonStyle.success if online else discord.ButtonStyle.secondary
        emoji = "🟢" if online else "🔴"
        btn   = discord.ui.Button(label=label, style=style, emoji=emoji, row=row, disabled=not online)

        async def callback(ix: discord.Interaction, _value=value, _label=label, _online=online):
            if not is_admin(ix):
                return await ix.response.send_message(
                    embed=em("⛔ Forbidden", "Admin only.", RED), ephemeral=True)
            if not _online:
                return await ix.response.send_message(
                    embed=em("❌ Node Offline", f"**{_label}** is offline. Pick an online node.", RED),
                    ephemeral=True)
            node_id = None if _value == "local" else _value
            await ix.response.send_modal(DeployModal(self.target, self.os_key, self.cpu_key, node_id))

        btn.callback = callback
        self.add_item(btn)

    async def _back(self, ix: discord.Interaction):
        await ix.response.edit_message(
            embed=em("🐉 Deploy — Step 2/4", "Choose **CPU**:", BLUE),
            view=CPUView(self.target, self.os_key),
        )

    async def _cancel(self, ix: discord.Interaction):
        await ix.response.edit_message(
            embed=em("❌ Cancelled", "Deployment cancelled.", RED), view=None)


@bot.tree.command(name="deploy", description="[Admin] 1-click VPS deploy.")
@app_commands.describe(user="User to deploy VPS for")
async def cmd_deploy(ix: discord.Interaction, user: discord.Member):
    if not is_admin(ix):
        return await ix.response.send_message(embed=em("⛔ Forbidden", "Admin only.", RED), ephemeral=True)
    await ix.response.send_message(
        embed=em("🐉 Deploy — Step 1/4",
                 f"Deploying for **{user.display_name}** ({user.mention})\n\nChoose **OS**:", BLUE),
        view=OSView(user), ephemeral=True,
    )

# ─────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN not set in .env!")
        raise SystemExit(1)
    if not PTERO_ON:
        log.warning("Pterodactyl not configured — running without panel integration.")
    else:
        log.info(f"Pterodactyl enabled — {PTERO_URL}")
    init_db()
    log.info("Starting RIVEN CLOUD VPS Manager...")
    bot.run(DISCORD_TOKEN, log_handler=None)
