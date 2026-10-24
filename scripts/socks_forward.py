#!/usr/bin/env python3
"""SOCKS5 egress through any devtool.py device, limited to an allow-list (REMOTE_ACCESS.md §2).

Some API keys only work from a site's public IP. The OpenAI key that bnu Home
Assistant uses (`BNU_HA_FRIGATE_OPENAI_API_KEY`) answers
`401 ip_not_authorized` from mia-desktop (2026-10-08). This opens a local SOCKS5
listener (CONNECT, no auth) whose connections leave through the SSH transport of
a fleet device (key first, .env password fallback — the same `ssh_client()`
devtool.py uses). Only the host:port pairs given on the command line are
tunnelled; everything else is refused.

    python scripts/socks_forward.py bnu-proxmox 18080 api.openai.com:443
    HTTPS_PROXY=socks5h://127.0.0.1:18080 python …   # requests needs PySocks

Keep it running in a background shell; it exits with code 1 if the SSH
transport dies.
"""
import os
import select
import socket
import socketserver
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devtool  # noqa: E402  (loads .env, provides ssh_client)

REFUSED = b"\x05\x02\x00\x01" + b"\x00" * 6
OK = b"\x05\x00\x00\x01" + b"\x00" * 6


def parse_dest(spec):
    host, port = spec.rsplit(":", 1)
    return host, int(port)


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    device, lport = sys.argv[1], int(sys.argv[2])
    allow = {parse_dest(s) for s in sys.argv[3:]}
    client = devtool.ssh_client(device)
    transport = client.get_transport()
    transport.set_keepalive(30)
    print(f"connected to {device}", flush=True)

    class Handler(socketserver.BaseRequestHandler):
        def recv_exact(self, n):
            buf = b""
            while len(buf) < n:
                chunk = self.request.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("eof during handshake")
                buf += chunk
            return buf

        def handle(self):
            s = self.request
            try:
                ver, nmethods = self.recv_exact(2)
                self.recv_exact(nmethods)
                if ver != 5:
                    return
                s.sendall(b"\x05\x00")                      # no auth
                _, cmd, _, atyp = self.recv_exact(4)
                if atyp == 3:                                # domain name
                    host = self.recv_exact(self.recv_exact(1)[0]).decode()
                elif atyp == 1:                              # IPv4
                    host = socket.inet_ntoa(self.recv_exact(4))
                else:
                    s.sendall(REFUSED)
                    return
                port = struct.unpack(">H", self.recv_exact(2))[0]
                if cmd != 1 or (host, port) not in allow:
                    print("refused", host, port, flush=True)
                    s.sendall(REFUSED)
                    return
                chan = transport.open_channel("direct-tcpip", (host, port), s.getpeername())
                s.sendall(OK)
            except Exception as e:  # noqa: BLE001
                print("handshake fail", e, flush=True)
                return
            try:
                while True:
                    readable, _, _ = select.select([s, chan], [], [], 120)
                    if not readable:
                        break
                    if s in readable:
                        data = s.recv(65536)
                        if not data:
                            break
                        chan.sendall(data)
                    if chan in readable:
                        data = chan.recv(65536)
                        if not data:
                            break
                        s.sendall(data)
            except Exception:  # noqa: BLE001
                pass
            finally:
                chan.close()
                s.close()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(("127.0.0.1", lport), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"socks5 127.0.0.1:{lport} -> {device}, allow {sorted(allow)}", flush=True)
    while True:
        time.sleep(30)
        if not transport.is_active():
            print("transport died", flush=True)
            os._exit(1)


if __name__ == "__main__":
    main()
