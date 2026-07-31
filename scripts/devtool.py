#!/usr/bin/env python3
"""devtool.py - THE one way to reach every device in the MiniStackableRack network.

If you are an LLM: do NOT invent your own SSH/API calls, and do NOT try several
methods until one works. This tool already encodes every credential lookup and
device quirk. See REMOTE_ACCESS.md at the repo root for the full access map.

Auth: key first (~/.ssh/id_ed25519, authorized on every Linux/Windows device),
password from the repo-root .env as fallback. plink/sshpass are NOT installed
on this machine; paramiko is.

Usage (run from anywhere):
  python scripts/devtool.py test [all|<region>|<device>]   # verify connectivity
  python scripts/devtool.py run  <device> <command...>     # run shell command over SSH
  python scripts/devtool.py push <device> <local> <remote> # copy local file to device
  python scripts/devtool.py pull <device> <remote> <local> # copy device file to local
  python scripts/devtool.py ha   <region> <METHOD> <path> [json-body]  # HA REST API
  python scripts/devtool.py list <region>                  # VMs/LXCs/containers of a rack
  python scripts/devtool.py guest <region> <vmid> <command...>  # run INSIDE a VM/LXC
  python scripts/devtool.py lan <region> <ip-or-name> <cmd|url> # reach a LAN-only device

  <region> = bnu | ply | bg | fln
  <device> = <region>-<component>, component =
             proxmox | homeassistant | raspberrypi | glkvm | win11 | nas
             (e.g. bnu-proxmox, ply-raspberrypi, bg-win11, ply-nas)

Examples:
  python scripts/devtool.py test all
  python scripts/devtool.py run bnu-proxmox "qm list"
  python scripts/devtool.py run bg-win11 "Get-Service sshd | Format-List"
  python scripts/devtool.py run ply-nas "sudo -S docker ps"
  python scripts/devtool.py list bnu
  python scripts/devtool.py guest bnu 101 "docker ps"          # LXC -> pct exec
  python scripts/devtool.py guest bnu 103 "Get-Process | select -First 3"  # VM -> guest agent
  python scripts/devtool.py lan bnu 10.1.1.132 http://10.1.1.132/  # Zigbee GW via host hop
  python scripts/devtool.py ha bnu GET /api/states/sun.sun
  python scripts/devtool.py push bnu-homeassistant ./automations.yaml /config/automations.yaml

Device facts this tool encodes (do not rediscover these):
  - All hosts are Tailscale MagicDNS names (<region>-<component>), port 22.
  - proxmox:        ssh root         - key auth OK, SFTP OK
  - raspberrypi:    ssh eduardocenci - key auth OK, SFTP OK
  - glkvm:          ssh root         - key auth OK (dropbear), SFTP may fail -> auto fallback
  - win11:          ssh eduardocenci - key auth OK, default shell is PowerShell, NO SFTP
  - nas:            ssh (PLY_NAS_SSH_LOGIN) - key auth OK; docker needs `sudo -S` with
                    PLY_NAS_SSH_PW piped in; compose v1 binary is /usr/local/bin/docker-compose
  - homeassistant:  prefer REST API (<REGION>_HA_URL + <REGION>_HA_TOKEN).
                    SSH is the "Advanced SSH & Web Terminal" add-on: user hassio
                    (HA_SSH_PW, password only - NO key auth), port 22, NO SFTP
                    subsystem, and /config is only writable via sudo -> push uses
                    `base64 -d | sudo tee`. After pushing package/helper YAML,
                    reload the right domain, e.g.
                    `ha <region> POST /api/services/automation/reload`.
  - Guests (VMs/LXCs) have no tailnet name of their own except win11 (and
    bnu-frigate): reach them through their Proxmox host with `list` / `guest`
    (pct exec for LXC, QEMU guest agent for VMs - enabled fleet-wide 2026-07-30).
  - NOT everything is on the tailnet. Zigbee gateways, the NVR, the doorbell,
    the routers and the bnu docker/ollama LXCs are LAN-only (10.1.1.x /
    192.168.0.x): their bare names do NOT resolve. Reach them with `lan`, which
    hops through the site's Proxmox host. See REMOTE_ACCESS.md section 2.
"""

import base64
import concurrent.futures
import json
import re
import shlex
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit("paramiko missing. Fix: pip install --user paramiko")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
KEY_FILE = Path.home() / ".ssh" / "id_ed25519"

REGIONS = ["bnu", "ply", "bg", "fln"]
COMPONENTS = {
    # component: (login_env_key, password_env_key)
    "proxmox": ("PROXMOX_LOGIN", "PROXMOX_PW"),
    "raspberrypi": ("RASPBERRYPI_LOGIN", "RASPBERRYPI_PW"),
    "glkvm": ("GLKVM_LOGIN", "GLKVM_PW"),
    "homeassistant": ("HA_SSH_LOGIN", "HA_SSH_PW"),
    "win11": (None, None),          # key auth only; user below
    "nas": ("PLY_NAS_SSH_LOGIN", "PLY_NAS_SSH_PW"),
}
# Components whose tailnet hostname is not "<region>-<component>".
HOSTNAME_OVERRIDES = {"nas": "{region}-nas-ds918plus"}
# Components that exist only at some sites.
ONLY_AT = {"nas": ["ply"]}
STATIC_USER = {"win11": "eduardocenci"}
NO_SFTP = {"homeassistant", "win11"}
SSH_TIMEOUT = 12


def load_env():
    if not ENV_FILE.exists():
        sys.exit(f"Missing {ENV_FILE}. Copy .env.example to .env and fill in credentials.")
    env = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
        if m:
            env[m.group(1)] = m.group(2).strip()
    return env


ENV = load_env()


def components_for(region):
    return [c for c in COMPONENTS if region in ONLY_AT.get(c, REGIONS)]


def parse_device(name):
    n = name.lower()
    region = comp = None
    # tolerate the full tailnet name (ply-nas-ds918plus) and the short alias (ply-nas)
    for c, tmpl in HOSTNAME_OVERRIDES.items():
        for r in REGIONS:
            if n == tmpl.format(region=r):
                region, comp = r, c
                break
        if comp:
            break
    if not comp:
        parts = n.split("-", 1)
        if len(parts) != 2 or parts[0] not in REGIONS or parts[1] not in COMPONENTS:
            sys.exit(
                f"Unknown device '{name}'. Expected <region>-<component>, "
                f"region in {REGIONS}, component in {list(COMPONENTS)}. "
                f"Guests (LXCs, non-Win11 VMs) and LAN-only devices are not reachable "
                f"directly - see REMOTE_ACCESS.md and `devtool.py list <region>`."
            )
        region, comp = parts
    # single validation point: both the alias and the full-name path land here
    if region not in ONLY_AT.get(comp, REGIONS):
        sys.exit(f"{comp} exists only at {ONLY_AT[comp]} - no {region}-{comp}.")
    return region, comp


def hostname_for(region, comp):
    return HOSTNAME_OVERRIDES.get(comp, "{region}-" + comp).format(region=region)


def ssh_client(device):
    region, comp = parse_device(device)
    host = hostname_for(region, comp)
    login_key, pw_key = COMPONENTS[comp]
    user = STATIC_USER.get(comp) or ENV.get(login_key)
    pw = ENV.get(pw_key) if pw_key else None
    if not user:
        raise RuntimeError(f"{login_key} missing in .env")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    common = dict(hostname=host, port=22, username=user, timeout=SSH_TIMEOUT,
                  banner_timeout=SSH_TIMEOUT, auth_timeout=SSH_TIMEOUT)
    # 1) key auth (works on every device except the HA add-on)
    if KEY_FILE.exists() and comp != "homeassistant":
        try:
            client.connect(key_filename=str(KEY_FILE), allow_agent=False,
                           look_for_keys=False, **common)
            return client
        except paramiko.AuthenticationException:
            pass
        except socket.gaierror:
            sys.exit(f"'{host}' does not resolve. Is it on the tailnet? "
                     f"(`tailscale status`; LAN-only devices need a hop - see REMOTE_ACCESS.md)")
        except (OSError, socket.timeout) as e:
            sys.exit(f"cannot reach {host}:22 - {e}")
    # 2) password from .env
    if not pw:
        raise RuntimeError(f"key auth failed for {host} and {pw_key} missing in .env")
    try:
        client.connect(password=pw, allow_agent=False, look_for_keys=False, **common)
    except socket.gaierror:
        sys.exit(f"'{host}' does not resolve. Is it on the tailnet? "
                 f"(`tailscale status`; LAN-only devices need a hop - see REMOTE_ACCESS.md)")
    return client


def ssh_run(device, command, input_bytes=None, timeout=120):
    """Run command; return (exit_code, stdout_str, stderr_str)."""
    client = ssh_client(device)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        if input_bytes is not None:
            stdin.write(input_bytes)
            stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err
    finally:
        client.close()


def push(device, local, remote):
    _, comp = parse_device(device)
    data = Path(local).read_bytes()
    if comp == "homeassistant":
        # No SFTP; hassio needs sudo for /config. base64 avoids quoting issues.
        b64 = base64.b64encode(data)
        code, out, err = ssh_run(
            device, f"base64 -d | sudo tee '{remote}' > /dev/null", input_bytes=b64)
        if code != 0:
            sys.exit(f"push failed ({code}): {err or out}")
    elif comp == "win11":
        # PowerShell default shell: decode base64 from stdin to the target path.
        b64 = base64.b64encode(data).decode()
        code, out, err = ssh_run(
            device,
            "powershell -NoProfile -Command "
            f"\"[IO.File]::WriteAllBytes('{remote}', [Convert]::FromBase64String('{b64}'))\"",
        )
        if code != 0:
            sys.exit(f"push failed ({code}): {err or out}")
    else:
        client = ssh_client(device)
        try:
            try:
                sftp = client.open_sftp()
                sftp.put(local, remote)
                sftp.close()
            except Exception:
                client.close()
                b64 = base64.b64encode(data)
                code, out, err = ssh_run(device, f"base64 -d > '{remote}'", input_bytes=b64)
                if code != 0:
                    sys.exit(f"push failed ({code}): {err or out}")
        finally:
            client.close()
    print(f"OK pushed {local} -> {device}:{remote} ({len(data)} bytes)")


def pull(device, remote, local):
    _, comp = parse_device(device)
    if comp == "win11":
        cmd = ("powershell -NoProfile -Command "
               f"\"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{remote}'))\"")
    else:
        cmd = f"base64 '{remote}'"
    code, out, err = ssh_run(device, cmd)
    if code != 0:
        sys.exit(f"pull failed ({code}): {err or out}")
    Path(local).write_bytes(base64.b64decode(out))
    print(f"OK pulled {device}:{remote} -> {local}")


def ha_api(region, method, path, body=None):
    region = region.lower()
    url_key, tok_key = f"{region.upper()}_HA_URL", f"{region.upper()}_HA_TOKEN"
    url, token = ENV.get(url_key), ENV.get(tok_key)
    if not url or not token:
        sys.exit(f"{url_key}/{tok_key} missing in .env - cannot reach {region} HA API")
    req = urllib.request.Request(
        url.rstrip("/") + path,
        method=method.upper(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else
             (b"{}" if method.upper() == "POST" else None),
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


# ------------------------------------------------------------- guests (VM/LXC)

def _guest_kind(region, vmid):
    """Return 'lxc' or 'vm' for a vmid on <region>-proxmox."""
    code, out, _ = ssh_run(f"{region}-proxmox", f"pct list | awk '$1==\"{vmid}\"'")
    if code == 0 and out.strip():
        return "lxc"
    code, out, _ = ssh_run(f"{region}-proxmox", f"qm list | awk '$1==\"{vmid}\"'")
    if code == 0 and out.strip():
        return "vm"
    sys.exit(f"vmid {vmid} not found on {region}-proxmox (try: devtool.py list {region})")


def cmd_list(region):
    host = f"{region}-proxmox"
    code, out, err = ssh_run(host, "qm list; echo '--- LXC ---'; pct list")
    print(out or err, end="")
    # containers inside each running LXC
    code, ids, _ = ssh_run(host, "pct list | awk 'NR>1 && $2==\"running\" {print $1}'")
    for vmid in [i for i in ids.split() if i.isdigit()]:
        c, o, _ = ssh_run(
            host, f"pct exec {vmid} -- docker ps --format '{{{{.Names}}}}\\t{{{{.Status}}}}'")
        if c == 0 and o.strip():
            print(f"--- docker in LXC {vmid} ---")
            print(o, end="")


def cmd_lan(region, target, command):
    """Reach a LAN-only device (Zigbee gateway, NVR, router, non-tailnet LXC) by
    hopping through that site's Proxmox host, which sits on both networks."""
    host = f"{region}-proxmox"
    if command.strip().startswith(("http://", "https://")):
        command = f"curl -sS --max-time 15 {shlex.quote(command.strip())}"
    elif not command.strip():
        command = f"ping -c 2 -W 2 {shlex.quote(target)}"
    code, out, err = ssh_run(host, command, timeout=120)
    print(out, end="")
    if err:
        print(err, end="", file=sys.stderr)
    sys.exit(code)


def cmd_guest(region, vmid, command):
    """Run a command inside a guest: pct exec for LXC, QEMU guest agent for VM."""
    host = f"{region}-proxmox"
    kind = _guest_kind(region, vmid)
    if kind == "lxc":
        code, out, err = ssh_run(host, f"pct exec {vmid} -- sh -c {shlex.quote(command)}")
        print(out, end="")
        if err:
            print(err, end="", file=sys.stderr)
        sys.exit(code)

    # VM: QEMU guest agent. Windows guests get PowerShell, Linux guests /bin/sh.
    code, ostype, _ = ssh_run(host, f"qm config {vmid} | awk '/^ostype:/ {{print $2}}'")
    is_win = ostype.strip().startswith("win")
    if is_win:
        # ProgressPreference off: PowerShell otherwise emits CLIXML progress on stderr.
        ps = "$ProgressPreference='SilentlyContinue'\n" + command
        b64 = base64.b64encode(ps.encode("utf-16-le")).decode()
        exec_args = ("powershell.exe -NoProfile -NonInteractive "
                     f"-ExecutionPolicy Bypass -EncodedCommand {b64}")
    else:
        exec_args = f"/bin/sh -c {shlex.quote(command)}"
    code, out, err = ssh_run(host, f"qm guest exec {vmid} --timeout 60 -- {exec_args}",
                             timeout=180)
    if code != 0 and "not running" in (err or "").lower():
        sys.exit(f"guest agent not responding on {region} vmid {vmid}: {err.strip()}")

    payload = out.strip()
    # Long-running commands: qm returns {"pid": N} - poll exec-status until exited.
    try:
        data = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        print(payload)
        sys.exit(code)
    if "pid" in data and "exited" not in data:
        pid = data["pid"]
        for _ in range(60):
            time.sleep(10)
            c2, o2, _ = ssh_run(host, f"qm guest exec-status {vmid} {pid}", timeout=60)
            try:
                st = json.loads(o2)
            except json.JSONDecodeError:
                continue
            if st.get("exited"):
                data = st
                break
        else:
            sys.exit(f"still running after 10 min (pid {pid}); "
                     f"poll: devtool.py run {host} 'qm guest exec-status {vmid} {pid}'")
    if data.get("out-data"):
        print(data["out-data"], end="")
    errtext = data.get("err-data", "")
    if errtext.lstrip().startswith("#< CLIXML"):   # PowerShell stream noise, not an error
        errtext = ""
    if errtext:
        print(errtext, end="", file=sys.stderr)
    sys.exit(data.get("exitcode", 0))


# ---------------------------------------------------------------- test mode

def check_ssh(device):
    try:
        _, comp = parse_device(device)
        probe = "hostname" if comp == "win11" else "echo ok && whoami && uname -a"
        code, out, err = ssh_run(device, probe)
        if code == 0 and out.strip():
            return True, out.strip().splitlines()[-1][:60]
        return False, (err or out).strip()[:80]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:80]


def check_ha_api(region):
    try:
        status, body = ha_api(region, "GET", "/api/")
        return status == 200, f"HTTP {status} {body[:40]}"
    except SystemExit as e:
        return False, str(e)[:80]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:80]


def cmd_test(target):
    checks = []  # (label, fn)
    regions = REGIONS if target in ("all", None) else (
        [target] if target in REGIONS else None)
    if regions:
        for r in regions:
            for c in components_for(r):
                checks.append((f"{r}-{c} [ssh]", lambda d=f"{r}-{c}": check_ssh(d)))
            checks.append((f"{r}-homeassistant [api]", lambda r=r: check_ha_api(r)))
    else:
        region, comp = parse_device(target)
        checks.append((f"{target} [ssh]", lambda: check_ssh(target)))
        if comp == "homeassistant":
            checks.append((f"{target} [api]", lambda: check_ha_api(region)))

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(fn): label for label, fn in checks}
        for fut in concurrent.futures.as_completed(futs):
            results[futs[fut]] = fut.result()

    failed = 0
    for label, _ in checks:
        ok, detail = results[label]
        mark = "OK  " if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"{mark} {label:32s} {detail}")
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)
    cmd = args[0]
    if cmd == "test":
        cmd_test(args[1] if len(args) > 1 else "all")
    elif cmd == "run" and len(args) >= 3:
        code, out, err = ssh_run(args[1], " ".join(args[2:]))
        if out:
            print(out, end="")
        if err:
            print(err, end="", file=sys.stderr)
        sys.exit(code)
    elif cmd == "push" and len(args) == 4:
        push(args[1], args[2], args[3])
    elif cmd == "pull" and len(args) == 4:
        pull(args[1], args[2], args[3])
    elif cmd == "ha" and len(args) >= 4:
        body = json.loads(args[4]) if len(args) > 4 else None
        status, text = ha_api(args[1], args[2], args[3], body)
        print(f"HTTP {status}")
        print(text)
    elif cmd == "list" and len(args) == 2:
        cmd_list(args[1].lower())
    elif cmd == "guest" and len(args) >= 4:
        cmd_guest(args[1].lower(), args[2], " ".join(args[3:]))
    elif cmd == "lan" and len(args) >= 3:
        cmd_lan(args[1].lower(), args[2], " ".join(args[3:]))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
