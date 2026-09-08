#!/usr/bin/env python3
"""Read what a WLED controller is actually showing, LED by LED.

WLED 16 removed GET /json/live (HTTP 501); the only readback is the websocket
"peek" (send {"lv":true}, receive binary frames: 'L', version, then RGB triplets).
The controllers are LAN-only, so the peek runs on the site's Proxmox host via
devtool.py lan and the result is summarised here.

    python scripts/wled_live.py mia 192.168.2.65          # per-segment summary
    python scripts/wled_live.py mia 192.168.2.65 --raw    # every LED as hex
"""
import base64, json, os, subprocess, sys

REMOTE = r'''
import socket, base64, os, struct, json, sys, time, urllib.request
IP = sys.argv[1]
def get(p): return json.loads(urllib.request.urlopen("http://%s%s" % (IP, p), timeout=5).read())
class R:
    def __init__(s, sock, buf=b""): s.sock, s.buf = sock, buf
    def exact(s, n):
        while len(s.buf) < n:
            c = s.sock.recv(4096)
            if not c: raise EOFError
            s.buf += c
        out, s.buf = s.buf[:n], s.buf[n:]
        return out
def frame(r):
    h = r.exact(2); op = h[0] & 0x0F; ln = h[1] & 0x7F
    if ln == 126: ln = struct.unpack(">H", r.exact(2))[0]
    elif ln == 127: ln = struct.unpack(">Q", r.exact(8))[0]
    if h[1] & 0x80: r.exact(4)
    return op, r.exact(ln)
def peek():
    sock = socket.create_connection((IP, 80), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(("GET /ws HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (IP, key)).encode())
    resp = b""
    while b"\r\n\r\n" not in resp: resp += sock.recv(4096)
    head, rest = resp.split(b"\r\n\r\n", 1)
    if b" 101 " not in head.split(b"\r\n")[0]: return None
    r = R(sock, rest)
    p = b'{"lv":true}'; m = os.urandom(4)
    sock.sendall(bytes([0x81, 0x80 | len(p)]) + m + bytes(b ^ m[i % 4] for i, b in enumerate(p)))
    t0 = time.time()
    while time.time() - t0 < 6:
        op, data = frame(r)
        if op == 2 and data[:1] == b"L":
            off = 2 if data[1] == 1 else 4; rgb = data[off:]; sock.close()
            return [rgb[i:i+3].hex().upper() for i in range(0, len(rgb) - len(rgb) % 3, 3)]
        if op == 8: break
    sock.close(); return None
st = get("/json/state"); info = get("/json/info")
print(json.dumps({"leds": peek(), "state": st, "count": info["leds"]["count"], "name": info.get("name")}))
'''

def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    site, ip, raw = sys.argv[1], sys.argv[2], "--raw" in sys.argv
    devtool = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devtool.py")
    b64 = base64.b64encode(REMOTE.encode()).decode()
    r = subprocess.run([sys.executable, devtool, "lan", site, ip,
                        f"echo {b64} | base64 -d > /tmp/wled_live.py && python3 /tmp/wled_live.py {ip}"],
                       capture_output=True, text=True, timeout=90)
    lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
    if r.returncode or not lines:
        sys.exit(f"devtool lan failed (rc {r.returncode}): {r.stderr.strip()[-400:] or r.stdout[-400:]}")
    d = json.loads(lines[-1]); leds, st = d["leds"], d["state"]
    if leds is None:
        sys.exit("no live frame from the websocket peek")
    if raw:
        print(json.dumps(leds)); return
    print(f"{d['name']} {ip}: {d['count']} LEDs, {'on' if st['on'] else 'off'}, bri {st['bri']}, preset {st.get('ps')}")
    covered = set()
    for s in st["seg"]:
        if "id" not in s: continue
        rng = range(s["start"], min(s["stop"], len(leds))); covered.update(rng)
        lit = [i for i in rng if leds[i] != "000000"]
        runs, start = [], None
        for i in rng:
            if leds[i] != "000000" and start is None: start = i
            if (leds[i] == "000000" or i == rng[-1]) and start is not None:
                runs.append(f"{start}-{i if leds[i] != '000000' else i - 1}"); start = None
        cols = sorted(set(leds[i] for i in lit))
        print(f"  seg {s['id']} {s.get('n') or '-':16} {s['start']:>3}-{s['stop']:<3} {'on ' if s['on'] else 'off'}{' frz' if s['frz'] else '    '} fx {s['fx']:<3}"
              f" lit {len(lit)}/{len(rng)} {' '.join(runs) or '-'}  colours {', '.join(cols[:4])}{' ...' if len(cols) > 4 else ''}")
    stray = [i for i in range(len(leds)) if i not in covered and leds[i] != "000000"]
    print(f"  LEDs outside every segment: {len(leds) - len(covered)}, of which lit: {stray or 'none'}")

if __name__ == "__main__":
    main()
