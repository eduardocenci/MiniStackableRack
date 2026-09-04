#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-kasa==0.10.2"]
# ///
"""Make the mia Kasa HS300 power strip cloud-free and keep it that way.

Run from anywhere with `uv run scripts/kasa/mia-hs300/hs300_local.py <cmd>`
(this machine reaches the mia rack LAN 192.168.2.0/24 through the tailnet).

  probe    read-only: discovery record (owner hash = which Kasa account owns
           it) + local KLAP handshake test with blank / factory-default creds.
  unbind   ONE-TIME: log in locally with MIA_KASA_LOGIN / MIA_KASA_PW from the
           repo-root .env, send `cnCloud.unbind`, then re-verify with default
           creds. After this the strip never asks for an account again.
  verify   connect with default creds (what HA does when it stores none) and
           list the outlets + live power. This is the steady state.

Why: the strip's local KLAP handshake derives its key from the Kasa account
it is bound to, so HA needs the account password and re-asks for it whenever
TP-Link touches auth. An unbound strip answers to the built-in default
credentials, so HA needs nothing stored (see REMOTE_ACCESS.md, mia section).
"""
from __future__ import annotations

import asyncio
import binascii
import hashlib
import http.client
import json
import os
import socket
import sys
from pathlib import Path

from kasa import Credentials, Device, DeviceConfig, Discover, Module

HOST = os.environ.get("HS300_HOST", "192.168.2.50")
ENV = Path(__file__).resolve().parents[3] / ".env"
DISCOVERY_V2 = binascii.unhexlify("020000010000000000000000463cb5d3")
CANDIDATES = {
    "blank": ("", ""),
    "kasa-default": ("kasa@tp-link.net", "kasaSetup"),
}
# Outlet index -> alias, as registered in mia HA on 2026-09-01 (HA unique_ids
# are <deviceid>0<index>, so entity ids survive a factory reset; aliases don't).
ALIASES = {
    0: "Plug1",
    1: "Anker_USBcHub",
    2: "Desktop",
    3: "MiniRack_Main",
    4: "MiniRack_UPS_NAS",
    5: "MiniRack_AUX_Rasp",
}


def env(key: str) -> str | None:
    if key in os.environ:
        return os.environ[key]
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ----------------------------------------------------------------- probe
def discovery_record() -> dict:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    s.sendto(DISCOVERY_V2, (HOST, 20002))
    data, _ = s.recvfrom(4096)
    return json.loads(data[16:])["result"]


def auth_hash(user: str, pw: str) -> bytes:
    return hashlib.sha256(
        hashlib.sha1(user.encode()).digest() + hashlib.sha1(pw.encode()).digest()
    ).digest()


def handshake_matches() -> dict[str, bool]:
    local_seed = os.urandom(16)
    c = http.client.HTTPConnection(HOST, 80, timeout=5)
    c.request("POST", "/app/handshake1", body=local_seed,
              headers={"Content-Type": "application/octet-stream"})
    body = c.getresponse().read()
    remote_seed, server_hash = body[:16], body[16:48]
    # KLAP v2 (python-kasa KlapTransportV2): sha256(local_seed + remote_seed + auth_hash)
    return {
        name: hashlib.sha256(local_seed + remote_seed + auth_hash(u, p)).digest() == server_hash
        for name, (u, p) in CANDIDATES.items()
    }


async def probe_legacy() -> int:
    """fw 1.0.x strips: no KLAP, no port-20002 discovery — ask over port 9999."""
    dev = await connect(Credentials(*CANDIDATES["kasa-default"]))
    cloud = dev.modules[Module.IotCloud].info
    print(f"model={dev.model} mac={dev.mac} fw={dev.hw_info.get('sw_ver')} encrypt=legacy(9999)")
    if cloud.provisioned:
        print(f"binded=1 username={cloud.username!r}  -> BOUND to a Kasa cloud account")
    else:
        print("binded=0  -> NOT bound to any cloud account")
    print(f"cloud_connected={cloud.cloud_connected} server={cloud.server!r}")
    show(dev)
    await dev.disconnect()
    return 0


def cmd_probe() -> int:
    try:
        r = discovery_record()
    except TimeoutError:
        return asyncio.run(probe_legacy())
    owner = r.get("owner") or ""
    print(f"model={r.get('device_model')} mac={r.get('mac')} "
          f"encrypt={r.get('mgt_encrypt_schm', {}).get('encrypt_type')} "
          f"factory_default={r.get('factory_default')}")
    if owner:
        me = env("MIA_KASA_LOGIN")
        who = " (= MIA_KASA_LOGIN)" if me and hashlib.md5(me.encode()).hexdigest().upper() == owner else ""
        print(f"owner={owner}{who}  -> BOUND to a Kasa cloud account")
    else:
        print("owner=<empty>  -> NOT bound to any cloud account")
    for name, ok in handshake_matches().items():
        print(f"  handshake with {name} creds: {'MATCH' if ok else 'no'}")
    return 0


# ----------------------------------------------------------------- connect
async def connect(creds: Credentials | None) -> Device:
    """python-kasa 0.10.2 maps IOT.KLAP to the v1 transport, but HS300 fw 1.1.x
    (lv=2, new_klap=1) speaks KLAP v2 — build the strip with KlapTransportV2."""
    from kasa.deviceconfig import DeviceConnectionParameters, DeviceEncryptionType, DeviceFamily
    from kasa.iot import IotStrip
    from kasa.protocols import IotProtocol
    from kasa.transports import KlapTransportV2

    try:
        lv = discovery_record().get("mgt_encrypt_schm", {}).get("lv")
    except TimeoutError:
        lv = None  # legacy fw (port 9999): normal discovery path works
    if lv == 2:
        cfg = DeviceConfig(
            host=HOST, credentials=creds,
            connection_type=DeviceConnectionParameters(
                DeviceFamily.IotSmartPlugSwitch, DeviceEncryptionType.Klap, login_version=2, https=False),
        )
        dev = IotStrip(HOST, config=cfg, protocol=IotProtocol(transport=KlapTransportV2(config=cfg)))
    else:
        dev = await Discover.discover_single(HOST, discovery_timeout=5, credentials=creds)
    await dev.update()
    return dev


def show(dev: Device) -> None:
    cloud = dev.modules.get(Module.IotCloud)
    print(f"alias={dev.alias} model={dev.model} fw={dev.hw_info.get('sw_ver')} "
          f"cloud_connected={cloud.is_connected if cloud else '?'}")
    for ch in dev.children:
        em = ch.modules.get(Module.Energy)
        w = f"{em.current_consumption:.1f} W" if em and em.current_consumption is not None else "-"
        print(f"  {ch.alias:<22} {'ON ' if ch.is_on else 'off'}  {w}")


async def cmd_verify() -> int:
    for name, (u, p) in CANDIDATES.items():
        try:
            dev = await connect(Credentials(u, p))
            print(f"connected with {name} creds")
            show(dev)
            await dev.disconnect()
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"{name} creds: {type(e).__name__}: {str(e)[:120]}")
    print("FAIL: strip still requires the Kasa account credentials")
    return 1


async def cmd_unbind() -> int:
    user, pw = env("MIA_KASA_LOGIN"), env("MIA_KASA_PW")
    if not (user and pw):
        print("MIA_KASA_LOGIN / MIA_KASA_PW missing from .env — add them first (see README)")
        return 2
    dev = await connect(Credentials(user, pw))
    print("logged in with the Kasa account:")
    show(dev)
    cloud = dev.modules[Module.IotCloud]
    print("sending cnCloud.unbind ...", await cloud.disconnect())
    await dev.disconnect()
    await asyncio.sleep(3)
    print("re-checking ownership:")
    cmd_probe()
    print("re-verifying with default creds:")
    return await cmd_verify()


async def cmd_names() -> int:
    """After a factory reset: strip alias + the six outlet aliases (default creds)."""
    dev = await connect(Credentials(*CANDIDATES["kasa-default"]))
    if dev.alias != "TP-LINK_Power Strip_F845":
        await dev.set_alias("TP-LINK_Power Strip_F845")
    for ch in dev.children:
        idx = int(ch.device_id[-2:])  # child id = <deviceid> + 2-digit index
        want = ALIASES.get(idx)
        if want and ch.alias != want:
            print(f"  outlet {idx}: {ch.alias!r} -> {want!r}")
            await ch.set_alias(want)
    await dev.update()
    show(dev)
    await dev.disconnect()
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if cmd == "probe":
        return cmd_probe()
    if cmd == "verify":
        return asyncio.run(cmd_verify())
    if cmd == "unbind":
        return asyncio.run(cmd_unbind())
    if cmd == "names":
        return asyncio.run(cmd_names())
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
