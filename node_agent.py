#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════╗
║           RIVEN CLOUD — Node Agent                     ║
║  Runs on a remote server and connects OUTBOUND to the  ║
║  main RIVEN CLOUD bot so it can host VPS containers on  ║
║  this machine too.                                     ║
╚═══════════════════════════════════════════════════════╝

Menu:
  1. Install VPS Bot     -> installs Docker + deps on this machine
  2. Uninstall VPS Bot   -> removes Docker + deps from this machine
  3. Connect NODE        -> paste the connect string from /node-config
                            and this machine starts hosting VPS for
                            the main bot
  4. Exit
"""

import os
import sys
import json
import time
import random
import string
import socket
import asyncio
import subprocess
import shlex
import shutil

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_config.json")


def ensure_deps():
    """Make sure 'docker' and 'aiohttp' Python packages are importable.
    If not, install them on the spot instead of crashing later."""
    missing = []
    for mod in ("docker", "aiohttp"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return True

    print(f"\n⚠️  Missing Python package(s): {', '.join(missing)} — installing now...\n")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "-q", *missing]
    )
    if r.returncode != 0:
        print(f"\n❌ Failed to auto-install {', '.join(missing)}.")
        print(f"   Try manually: {sys.executable} -m pip install --break-system-packages {' '.join(missing)}")
        return False

    still_missing = []
    for mod in missing:
        try:
            __import__(mod)
        except ImportError:
            still_missing.append(mod)
    if still_missing:
        print(f"\n❌ Still missing after install: {', '.join(still_missing)}")
        print("   Choose '1. Install VPS Bot' from the menu first, then try again.")
        return False

    print("✅ Dependencies installed.\n")
    return True


# ─────────────────────────────────────────────────────
# 1. INSTALL / UNINSTALL
# ─────────────────────────────────────────────────────
def run(cmd: str, check: bool = True):
    print(f"  $ {cmd}")
    r = subprocess.run(cmd, shell=True)
    if check and r.returncode != 0:
        print(f"  ⚠️  Command exited with code {r.returncode} (continuing anyway)")
    return r.returncode


def install_vps_bot():
    print("\n=== Installing Docker + Incus + agent dependencies ===\n")
    run("curl -fsSL https://get.docker.com | sh", check=False)
    run("systemctl enable docker", check=False)
    run("systemctl start docker", check=False)
    run("apt-get update -qq", check=False)
    run("apt-get install -y -qq python3 python3-pip python3-venv incus", check=False)
    run("systemctl enable incus", check=False)
    run("systemctl start incus", check=False)
    run("incus admin init --auto", check=False)
    run(f"{sys.executable} -m pip install --break-system-packages -q docker aiohttp", check=False)
    print("\n✅ Docker + Incus + dependencies installed. Verify with: docker ps && incus list")
    print("Next: run this script again and choose '3. Connect NODE'.\n")


def uninstall_vps_bot():
    print("\n=== Uninstalling ===\n")
    confirm = input("This removes Docker and ALL containers on this machine. Type 'yes' to confirm: ")
    if confirm.strip().lower() != "yes":
        print("Cancelled.")
        return
    run("docker ps -aq --filter label=managed-by=rivencloud | xargs -r docker rm -f", check=False)
    run("apt-get purge -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin "
        "docker-compose-plugin", check=False)
    run("rm -rf /var/lib/docker", check=False)
    if os.path.exists(CONFIG_FILE):
        os.remove(CONFIG_FILE)
    print("\n✅ Uninstalled.\n")


# ─────────────────────────────────────────────────────
# 2. VPS PROVISIONING (mirrors the main bot's provision())
# ─────────────────────────────────────────────────────
def gen_root_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(length))


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def provision_vps(job: dict) -> dict:
    """Runs entirely on THIS machine's local Docker. Returns a job_result dict."""
    import docker

    vps_id     = job["vps_id"]
    image      = job["image"]
    ram_mb     = job["ram_mb"]
    cpu_cores  = job["cpu_cores"]
    cpu_name   = job.get("cpu_name", "Generic CPU")
    os_label   = job.get("os_label", image)
    host_port  = job["host_port"]
    root_pass  = job["root_pass"]

    # Make sure the requested port is actually free on THIS machine;
    # if not, pick another one in a small range around it.
    if not _port_free(host_port):
        for _ in range(20):
            candidate = random.randint(20000, 29999)
            if _port_free(candidate):
                host_port = candidate
                break
        else:
            return {"ok": False, "error": "No free port available on this node."}

    client = docker.from_env()
    client.images.pull(image)

    mem    = ram_mb * 1024 * 1024
    period = 100000
    quota  = int(cpu_cores * period)

    try:
        host_cfg = client.api.create_host_config(
            mem_limit=mem, memswap_limit=mem, cpu_period=period, cpu_quota=quota,
            privileged=True, cgroupns="host",
            binds={"/sys/fs/cgroup": {"bind": "/sys/fs/cgroup", "mode": "rw"}},
            tmpfs={"/run": "rw,nosuid,nodev", "/run/lock": "rw,nosuid,nodev", "/tmp": "rw,nosuid,nodev"},
            port_bindings={22: host_port},
        )
    except TypeError:
        host_cfg = client.api.create_host_config(
            mem_limit=mem, memswap_limit=mem, cpu_period=period, cpu_quota=quota,
            privileged=True,
            binds={"/sys/fs/cgroup": {"bind": "/sys/fs/cgroup", "mode": "rw"}},
            tmpfs={"/run": "rw,nosuid,nodev", "/run/lock": "rw,nosuid,nodev", "/tmp": "rw,nosuid,nodev"},
            port_bindings={22: host_port},
        )

    try:
        ct_data = client.api.create_container(
            image=image, name=vps_id, detach=True, tty=True, stdin_open=True,
            environment={"TERM": "xterm-256color", "container": "docker"},
            command="/sbin/init", host_config=host_cfg, ports=[22],
            labels={"managed-by": "rivencloud", "vps-id": vps_id},
        )
        client.api.start(ct_data["Id"])
        ct = client.containers.get(ct_data["Id"])
    except Exception as e:
        return {"ok": False, "error": f"Container create failed: {e}"}

    time.sleep(6)  # let systemd boot

    ct.exec_run("bash -c 'apt-get update -qq'", tty=False)
    ct.exec_run(
        "bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "openssh-server tmate neofetch curl wget sudo procps net-tools iproute2 htop'",
        tty=False,
    )

    # Fake specs so neofetch/free/lscpu show the "sold" resources
    ct.exec_run(f"bash -c \"echo -e 'MemTotal: {ram_mb*1024} kB' > /proc/meminfo\"", tty=False)

    # Root password + direct SSH
    ct.exec_run(f"bash -c \"echo 'root:{root_pass}' | chpasswd\"", tty=False)
    ct.exec_run("mkdir -p /run/sshd", tty=False)
    ct.exec_run(
        "bash -c \""
        "sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config; "
        "sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "grep -q '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config; "
        "grep -q '^PasswordAuthentication' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config"
        "\"",
        tty=False,
    )
    ct.exec_run(
        "bash -c 'systemctl enable ssh 2>/dev/null; systemctl restart ssh "
        "|| systemctl restart sshd || service ssh restart'",
        tty=False,
    )

    # tmate backup SSH
    sock = "/tmp/tmate.sock"
    ct.exec_run(f"bash -c 'rm -f {sock}; tmate -S {sock} new-session -d'", tty=False)
    time.sleep(5)
    ct.exec_run(f"bash -c 'tmate -S {sock} wait tmate-ready'", tty=False)
    r   = ct.exec_run(f"bash -c \"tmate -S {sock} display -p '#{{tmate_ssh}}'\"", tty=False)
    ssh = r.output.decode(errors="ignore").strip() if r.output else ""

    return {"ok": True, "container_id": ct.id, "ssh": ssh, "host_port": host_port}



def provision_lxc_vps(job: dict) -> dict:
    """Create an Incus LXC system container with root SSH on this node."""
    vps_id = job["vps_id"]
    images = {
        "jrei/systemd-ubuntu:20.04": "images:ubuntu/20.04",
        "jrei/systemd-ubuntu:22.04": "images:ubuntu/22.04",
        "jrei/systemd-ubuntu:24.04": "images:ubuntu/24.04",
        "jrei/systemd-debian:11": "images:debian/11",
        "jrei/systemd-debian:12": "images:debian/12",
    }
    image = images.get(job["image"], job["image"])
    ram, cpu, disk = int(job["ram_mb"]), float(job["cpu_cores"]), int(job["disk_gb"])
    port, password = int(job["host_port"]), job["root_pass"]

    def incus(*args, timeout=900, check=True):
        p = subprocess.run(["incus", *map(str, args)], text=True, capture_output=True, timeout=timeout)
        if check and p.returncode:
            raise RuntimeError((p.stderr or p.stdout or "Incus command failed").strip())
        return p

    if not shutil.which("incus"):
        return {"ok": False, "error": "Incus is not installed on this node. Run install option first."}
    if not _port_free(port):
        return {"ok": False, "error": f"SSH port {port} is already in use."}
    subprocess.run(["iptables", "-I", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"], capture_output=True)
    incus("launch", image, vps_id, "--config", f"limits.memory={ram}MiB",
          "--config", f"limits.cpu={max(1, int(cpu + 0.999))}",
          "--config", f"limits.cpu.allowance={cpu * 100:g}%")
    try:
        incus("config", "device", "set", vps_id, "root", "size", f"{disk}GiB")
        setup = (
            "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && "
            "apt-get install -y -qq openssh-server tmate sudo curl wget procps iproute2 net-tools htop; "
            f"echo {shlex.quote('root:'+password)} | chpasswd; "
            "mkdir -p /run/sshd; "
            "sed -i 's/^#\\\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config; "
            "sed -i 's/^#\\\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
            "grep -q '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config; "
            "grep -q '^PasswordAuthentication' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config; "
            "systemctl enable ssh; systemctl restart ssh"
        )
        incus("exec", vps_id, "--", "bash", "-lc", setup)
        guest_ips = incus("exec", vps_id, "--", "bash", "-lc", "hostname -I").stdout.split()
        guest_ip = next((ip for ip in guest_ips if ":" not in ip), "")
        if not guest_ip:
            raise RuntimeError("Incus container has no IPv4 address; check the Incus bridge/profile.")
        incus("config", "device", "add", vps_id, "rivencloud-ssh", "proxy",
              f"listen=tcp:0.0.0.0:{port}", f"connect=tcp:{guest_ip}:22", "bind=host")
        incus("config", "set", vps_id, "user.rivencloud.managed", "true")
    except Exception:
        incus("delete", vps_id, "--force", check=False)
        raise
    # tmate is a backup channel, matching the existing Docker runtime.
    incus("exec", vps_id, "--", "bash", "-lc",
          "pkill tmate 2>/dev/null || true; rm -f /tmp/tmate.sock; "
          "tmate -S /tmp/tmate.sock new-session -d; tmate -S /tmp/tmate.sock wait tmate-ready",
          timeout=90)
    backup = incus("exec", vps_id, "--", "bash", "-lc",
                   "tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'", timeout=60).stdout.strip()
    return {"ok": True, "container_id": vps_id, "ssh": backup, "host_port": port}


def exec_action(job: dict) -> dict:
    """Handle lifecycle actions for Docker or Incus VPS instances."""
    if job.get("backend", "docker") == "incus":
        actions = {"start": ["start"], "stop": ["stop"], "restart": ["restart"], "remove": ["delete", "--force"]}
        action, vps_id = job.get("action"), job.get("vps_id")
        if action not in actions:
            return {"ok": False, "error": f"Unknown Incus action '{action}'"}
        p = subprocess.run(["incus", *actions[action], vps_id], text=True, capture_output=True, timeout=300)
        return {"ok": p.returncode == 0, "error": p.stderr.strip() if p.returncode else ""}
    import docker
    client = docker.from_env()
    action = job.get("action")
    vps_id = job.get("vps_id")
    try:
        ct = client.containers.get(vps_id)
        if action == "start":
            ct.start()
        elif action == "stop":
            ct.stop()
        elif action == "restart":
            ct.restart()
        elif action == "remove":
            ct.remove(force=True, v=True)
        else:
            return {"ok": False, "error": f"Unknown action '{action}'"}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────
# 3. CONNECT NODE — persistent WebSocket client
# ─────────────────────────────────────────────────────
def save_config(node_id, token, server_ip, port):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"node_id": node_id, "token": token, "server_ip": server_ip, "port": port}, f)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE) as f:
        return json.load(f)


def parse_connect_string(s: str):
    """Format from /node-config: node_id|token|server_ip|port"""
    parts = s.strip().split("|")
    if len(parts) != 4:
        raise ValueError("That doesn't look like a valid connect string (expected 4 parts separated by '|').")
    node_id, token, server_ip, port = parts
    return node_id, token, server_ip, int(port)


async def agent_loop(node_id, token, server_ip, port):
    import aiohttp

    url = f"ws://{server_ip}:{port}/agent/ws"
    backoff = 5
    while True:
        try:
            print(f"[agent] Connecting to {url} as '{node_id}'...")
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, heartbeat=30) as ws:
                    await ws.send_json({"type": "hello", "node_id": node_id, "token": token})
                    ack = await ws.receive_json()
                    if not ack.get("ok"):
                        print(f"[agent] ❌ Rejected: {ack.get('error')}")
                        print("[agent] Check your connect string is correct and try again.")
                        return
                    print("[agent] ✅ Connected. Waiting for jobs...")
                    backoff = 5

                    async for msg in ws:
                        if msg.type.name != "TEXT":
                            continue
                        job = json.loads(msg.data)
                        jtype = job.get("type")
                        print(f"[agent] Job received: {jtype} ({job.get('vps_id')})")

                        try:
                            loop = asyncio.get_running_loop()
                            if jtype == "create_vps":
                                result = await loop.run_in_executor(None, provision_vps, job)
                            elif jtype == "create_lxc_vps":
                                result = await loop.run_in_executor(None, provision_lxc_vps, job)
                            elif jtype == "exec_action":
                                result = await loop.run_in_executor(None, exec_action, job)
                            else:
                                result = {"ok": False, "error": f"Unknown job type '{jtype}'"}
                        except Exception as job_err:
                            print(f"[agent] ❌ Job error: {job_err}")
                            result = {"ok": False, "error": str(job_err)}

                        result["type"] = "job_result"
                        result["job_id"] = job["job_id"]
                        await ws.send_json(result)
        except Exception as e:
            print(f"[agent] Connection lost/failed: {e}")
        print(f"[agent] Reconnecting in {backoff}s...")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


def connect_node():
    if not ensure_deps():
        return
    existing = load_config()
    if existing:
        print(f"\nAlready configured as node '{existing['node_id']}' -> {existing['server_ip']}:{existing['port']}")
        again = input("Paste a NEW connect string to reconfigure, or press Enter to reuse this one: ").strip()
        if again:
            node_id, token, server_ip, port = parse_connect_string(again)
            save_config(node_id, token, server_ip, port)
        else:
            node_id, token, server_ip, port = (
                existing["node_id"], existing["token"], existing["server_ip"], existing["port"]
            )
    else:
        raw = input("\nPaste the connect string from /node-config: ").strip()
        try:
            node_id, token, server_ip, port = parse_connect_string(raw)
        except ValueError as e:
            print(f"❌ {e}")
            return
        save_config(node_id, token, server_ip, port)

    print(
        "\n⚠️  This process must stay running for the node to stay online.\n"
        "   To keep it running after you close this terminal, use tmux/screen,\n"
        "   or set it up as a systemd service (see the README).\n"
    )
    try:
        asyncio.run(agent_loop(node_id, token, server_ip, port))
    except KeyboardInterrupt:
        print("\n[agent] Stopped.")


# ─────────────────────────────────────────────────────
# 4. MENU
# ─────────────────────────────────────────────────────
def menu():
    while True:
        print("\n" + "=" * 45)
        print("        RIVEN CLOUD — Node Agent")
        print("=" * 45)
        cfg = load_config()
        if cfg:
            print(f"  Configured as: {cfg['node_id']} -> {cfg['server_ip']}:{cfg['port']}")
        print("""
  1. Install VPS Bot
  2. Uninstall VPS Bot
  3. Connect NODE
  4. Exit
""")
        choice = input("Select an option [1-4]: ").strip()
        if choice == "1":
            install_vps_bot()
        elif choice == "2":
            uninstall_vps_bot()
        elif choice == "3":
            connect_node()
        elif choice == "4":
            print("Bye!")
            break
        else:
            print("Invalid option, try again.")


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("⚠️  Please run this script as root (sudo python3 node_agent.py).")
    menu()
