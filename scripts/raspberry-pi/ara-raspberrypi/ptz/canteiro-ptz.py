#!/usr/bin/env python3
"""canteiro-ptz — remote aiming of the ARA canteiro camera's PT lens.

Intelbras iM9-M (Dahua/Imou OEM) @ CAM_HOST, ONVIF on port 80 (Profile000 =
PT lens = RTSP channel 1 = relay path `canteiro`). Empirical findings
(2026-08-24, fw 2.800.00IB00N.0.R):

  - ContinuousMove WORKS (the only movement operation that does).
  - GetStatus returns a FAKE constant position (always 0.8, 0.8).
  - AbsoluteMove / RelativeMove / presets / home: NotImplemented or inert.

Therefore there is no reliable ONVIF "goto preset" on this firmware — the
Mibo app favorites live in Imou's cloud only. This tool does what IS
possible: velocity nudges plus snapshots, so you can aim remotely by eye.

Usage:
  canteiro-ptz move <vx> <vy> [s]   nudge: velocities -1..1 (x: + pans one
                                    way, - the other; y: tilt), s seconds
                                    (default 0.5), then stop
  canteiro-ptz snap [file]          keyframe snapshot of the PT lens via the
                                    local relay (default /tmp/canteiro-snap.jpg)
  canteiro-ptz stop                 emergency Stop
  canteiro-ptz pos                  prints the camera's (fake) position — kept
                                    only to document the firmware behavior

Credentials: /etc/canteiro-ptz.env (CAM_HOST, CAM_USER, CAM_PASS — the
camera device password, .env ARA_CANTEIRO_CAM_KEY). SOAP faults arrive as
HTTP 200 on this firmware; bodies are inspected for <Fault>.
"""
import base64
import datetime
import hashlib
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ENV_FILE = "/etc/canteiro-ptz.env"
PROFILE = "Profile000"          # PT lens; the fixed lens has no PTZ config
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
PTZ_NS = 'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
RELAY_URL = "rtsp://127.0.0.1:8554/canteiro"


def load_env():
    cfg = {"CAM_HOST": "192.168.1.56", "CAM_USER": "admin"}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    for k in cfg:
        cfg[k] = os.environ.get(k, cfg[k])
    if not cfg.get("CAM_PASS"):
        sys.exit("CAM_PASS not set (%s missing or incomplete)" % ENV_FILE)
    return cfg


CFG = load_env()
PTZ_XADDR = "http://%s/onvif/ptz_service" % CFG["CAM_HOST"]


def security_header():
    nonce = os.urandom(16)
    now = datetime.datetime.now(datetime.timezone.utc)
    created = now.strftime("%Y-%m-%dT%H:%M:%S.") + ("%03dZ" % (now.microsecond // 1000))
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + CFG["CAM_PASS"].encode()).digest()
    ).decode()
    return (
        '<s:Header><wsse:Security s:mustUnderstand="1"'
        ' xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"'
        ' xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        "<wsse:UsernameToken><wsse:Username>%s</wsse:Username>"
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">%s</wsse:Password>'
        '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">%s</wsse:Nonce>'
        "<wsu:Created>%s</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>"
        % (CFG["CAM_USER"], digest, base64.b64encode(nonce).decode(), created)
    )


def soap(body, timeout=12):
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        + security_header() + "<s:Body>" + body + "</s:Body></s:Envelope>"
    )
    req = urllib.request.Request(
        PTZ_XADDR, data=envelope.encode(),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        sys.exit("HTTP %s from camera: %s" % (e.code, " ".join(text.split())[:300]))
    except Exception as e:
        sys.exit("camera unreachable: %s" % e)
    if "Fault>" in text:  # faults arrive as HTTP 200 on this firmware
        sys.exit("camera SOAP fault: %s" % " ".join(text.split())[:700])
    return ET.fromstring(text)


def ptz_stop():
    soap(
        "<tptz:Stop %s><tptz:ProfileToken>%s</tptz:ProfileToken>"
        "<tptz:PanTilt>true</tptz:PanTilt></tptz:Stop>" % (PTZ_NS, PROFILE)
    )


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"

    if cmd == "move" and len(args) in (3, 4):
        vx, vy = float(args[1]), float(args[2])
        seconds = float(args[3]) if len(args) == 4 else 0.5
        soap(
            "<tptz:ContinuousMove %s><tptz:ProfileToken>%s</tptz:ProfileToken>"
            '<tptz:Velocity><PanTilt x="%.4f" y="%.4f" xmlns="%s"/></tptz:Velocity>'
            "</tptz:ContinuousMove>" % (PTZ_NS, PROFILE, vx, vy, SCHEMA_NS)
        )
        time.sleep(seconds)
        ptz_stop()
        print("moved (vx=%.2f vy=%.2f for %.1fs). Use 'snap' to see the result." % (vx, vy, seconds))
    elif cmd == "snap":
        out = args[1] if len(args) > 1 else "/tmp/canteiro-snap.jpg"
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-rtsp_transport", "tcp",
             "-skip_frame", "nokey", "-i", RELAY_URL, "-frames:v", "1", out],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            sys.exit("snapshot failed: %s" % r.stderr.strip()[:200])
        print(out)
    elif cmd == "stop":
        ptz_stop()
        print("stopped")
    elif cmd == "pos":
        root = soap(
            "<tptz:GetStatus %s><tptz:ProfileToken>%s</tptz:ProfileToken></tptz:GetStatus>"
            % (PTZ_NS, PROFILE)
        )
        pt = root.find(".//{*}Position/{*}PanTilt")
        print("pan=%s tilt=%s  (WARNING: this firmware always reports the same"
              " fake value — do not use for control)"
              % (pt.get("x") if pt is not None else "?", pt.get("y") if pt is not None else "?"))
    else:
        print(__doc__.strip())
        sys.exit(0 if cmd in ("help", "-h", "--help") else 2)


if __name__ == "__main__":
    main()
