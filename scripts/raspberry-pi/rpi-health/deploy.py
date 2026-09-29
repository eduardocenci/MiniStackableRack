#!/usr/bin/env python3
"""Roll rpi-health out to one or more Pis from this machine.

    python scripts/raspberry-pi/rpi-health/deploy.py bg mia bnu
    python scripts/raspberry-pi/rpi-health/deploy.py ara          # plain ssh to ara-raspberrypi2

Tars this folder in memory (CRLF stripped — the checkout is CRLF, see
REMOTE_ACCESS.md), ships it over devtool's SSH as stdin (`devtool.py push`
mangles long Git-Bash paths), runs install.sh as root (`sudo -S` with
RASPBERRYPI_PW on fln, where sudo asks) and prints the rpi_ metrics the
exporter serves afterwards. Safe to re-run: install.sh is idempotent.
"""
import io
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # scripts/
import devtool  # noqa: E402

FILES = ("rpi-health.sh", "rpi-health.service", "install.sh")
UNPACK = "rm -rf /tmp/rpi-health && mkdir -p /tmp/rpi-health && tar xz -C /tmp/rpi-health"
INSTALL = "bash /tmp/rpi-health/install.sh"


def bundle() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in FILES:
            data = (HERE / name).read_bytes().replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def deploy_devtool(site: str, tgz: bytes) -> int:
    dev = f"{site}-raspberrypi"
    code, out, err = devtool.ssh_run(dev, UNPACK, input_bytes=tgz)
    if code != 0:
        print(f"{dev}: unpack failed ({code}): {err or out}")
        return code
    code, out, err = devtool.ssh_run(dev, "sudo -n true")
    if code == 0:
        code, out, err = devtool.ssh_run(dev, f"sudo {INSTALL}")
    else:  # fln: sudo wants a password
        pw = devtool.ENV.get("RASPBERRYPI_PW", "")
        code, out, err = devtool.ssh_run(dev, f"sudo -S {INSTALL}", input_bytes=(pw + "\n").encode())
    print(out.strip() or err.strip())
    return code


def deploy_ssh(host: str, tgz: bytes) -> int:
    """ara is not in devtool.py: plain OpenSSH with the key (BatchMode)."""
    base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", f"eduardocenci@{host}"]
    r = subprocess.run(base + [UNPACK], input=tgz)
    if r.returncode != 0:
        return r.returncode
    r = subprocess.run(base + [f"sudo {INSTALL}"])
    return r.returncode


def main() -> None:
    sites = sys.argv[1:] or sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # systemctl's ● vs the cp1252 console
    tgz = bundle()
    failed = []
    for site in sites:
        print(f"=== {site}")
        rc = deploy_ssh("ara-raspberrypi2", tgz) if site == "ara" else deploy_devtool(site, tgz)
        if rc != 0:
            failed.append(site)
    sys.exit(f"failed: {', '.join(failed)}" if failed else 0)


if __name__ == "__main__":
    main()
