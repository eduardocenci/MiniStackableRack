#!/usr/bin/env python3
"""devtool.py - THE one way to reach every device in the MiniStackableRack network.

If you are an LLM: do NOT invent your own SSH/API calls. Use this tool.
It already encodes every credential lookup and device quirk. plink/sshpass are
NOT installed on this machine; interactive `ssh` does not work non-interactively.
This tool uses paramiko (installed) and the repo-root .env (gitignored).

Usage (run from repo root):
  python scripts/devtool.py test [all|<region>|<device>]   # verify connectivity
  python scripts/devtool.py run  <device> <command...>     # run shell command over SSH
  python scripts/devtool.py push <device> <local> <remote> # copy local file to device
  python scripts/devtool.py pull <device> <remote> <local> # copy device file to local
  python scripts/devtool.py ha   <region> <METHOD> <path> [json-body]  # HA REST API

  <region> = bnu | ply | bg | fln
  <device> = <region>-<component>, component = proxmox | homeassistant | raspberrypi | glkvm
             (e.g. bnu-proxmox, ply-raspberrypi, bg-homeassistant, fln-glkvm)

Examples:
  python scripts/devtool.py test all
  python scripts/devtool.py run bnu-proxmox "qm list"
  python scripts/devtool.py run bg-raspberrypi "docker ps"
  python scripts/devtool.py push bnu-homeassistant ./automations.yaml /config/automations.yaml
  python scripts/devtool.py ha bnu GET /api/states/sun.sun
  python scripts/devtool.py ha bnu POST /api/services/input_boolean/reload
  python scripts/devtool.py ha bnu POST /api/services/homeassistant/reload_all

Device facts this tool encodes (do not rediscover these):
  - All hosts are reached via Tailscale MagicDNS names (<region>-<component>), port 22.
  - proxmox:        ssh root      (PROXMOX_PW)          - SFTP works
  - raspberrypi:    ssh eduardocenci (RASPBERRYPI_PW)   - SFTP works
  - glkvm:          ssh root      (GLKVM_PW)            - SFTP may not work -> auto fallback
  - homeassistant:  prefer REST API (<REGION>_HA_URL + <REGION>_HA_TOKEN).
                    SSH is the "Advanced SSH & Web Terminal" add-on: user hassio
                    (HA_SSH_PW), port 22, NO SFTP subsystem, and /config is only
                    writable via sudo -> push uses `base64 -d | sudo tee`.
                    After pushing package/helper YAML, reload the right domain,
                    e.g. `ha <region> POST /api/services/automation/reload`.
"""

import base64
import concurrent.futures
import json
import posixpath
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit("paramiko missing. Fix: pip install --user paramiko")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

REGIONS = ["bnu", "ply", "bg", "fln"]
COMPONENTS = {
    # component: (login_env_key, password_env_key)
    "proxmox": ("PROXMOX_LOGIN", "PROXMOX_PW"),
    "raspberrypi": ("RASPBERRYPI_LOGIN", "RASPBERRYPI_PW"),
    "glkvm": ("GLKVM_LOGIN", "GLKVM_PW"),
    "homeassistant": ("HA_SSH_LOGIN", "HA_SSH_PW"),
}
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


def parse_device(name):
    parts = name.lower().split("-", 1)
    if len(parts) != 2 or parts[0] not in REGIONS or parts[1] not in COMPONENTS:
        sys.exit(
            f"Unknown device '{name}'. Expected <region>-<component>, "
            f"region in {REGIONS}, component in {list(COMPONENTS)}."
        )
    return parts[0], parts[1]


def ssh_client(device):
    region, comp = parse_device(device)
    login_key, pw_key = COMPONENTS[comp]
    user, pw = ENV.get(login_key), ENV.get(pw_key)
    if not user or not pw:
        raise RuntimeError(f"{login_key}/{pw_key} missing in .env")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=device, port=22, username=user, password=pw,
        timeout=SSH_TIMEOUT, banner_timeout=SSH_TIMEOUT, auth_timeout=SSH_TIMEOUT,
        allow_agent=False, look_for_keys=False,
    )
    return client


def ssh_run(device, command, input_bytes=None):
    """Run command; return (exit_code, stdout_str, stderr_str)."""
    client = ssh_client(device)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=120)
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
            device,
            f"base64 -d | sudo tee '{remote}' > /dev/null",
            input_bytes=b64,
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
                code, out, err = ssh_run(
                    device, f"base64 -d > '{remote}'", input_bytes=b64
                )
                if code != 0:
                    sys.exit(f"push failed ({code}): {err or out}")
        finally:
            client.close()
    print(f"OK pushed {local} -> {device}:{remote} ({len(data)} bytes)")


def pull(device, remote, local):
    code, out, err = ssh_run(device, f"base64 '{remote}'")
    if code != 0:
        sys.exit(f"pull failed ({code}): {err or out}")
    Path(local).write_bytes(base64.b64decode(out))
    print(f"OK pulled {device}:{remote} -> {local}")


def ha_api(region, method, path, body=None):
    region = region.lower()
    url_key, tok_key = f"{region.upper()}_HA_URL", f"{region.upper()}_HA_TOKEN"
    url, token = ENV.get(url_key), ENV.get(tok_key)
    if not url or not token:
        sys.exit(f"{url_key}/{tok_key} missing in .env — cannot reach {region} HA API")
    req = urllib.request.Request(
        url.rstrip("/") + path,
        method=method.upper(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else
             (b"{}" if method.upper() == "POST" else None),
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- test mode

def check_ssh(device):
    try:
        code, out, err = ssh_run(device, "echo ok && whoami && uname -a")
        if code == 0 and "ok" in out:
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
            for c in COMPONENTS:
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
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
