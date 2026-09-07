"""bnu LAN port-forwarder for off-site finance-hangar runs (REMOTE_ACCESS.md §2).

Forwards 127.0.0.1:18788 -> 10.1.1.126:8788 (waha-listener) and
127.0.0.1:13000 -> 10.1.1.126:3000 (WAHA gateway) through bnu-proxmox with
paramiko (key first, PROXMOX_PW fallback). Keep it running in a background
shell, then run the pipeline with:

    BNU_WAHA_LISTENER_URL=http://127.0.0.1:18788 BNU_WAHA_API_URL=http://127.0.0.1:13000

Usage (Git Bash):  nohup python scripts/bnu_lan_forward.py > /tmp/fwd.log 2>&1 &
Added 2026-09-07 (first off-LAN wa-sweep from this machine).
"""
import os, sys, socketserver, threading, select, socket, time
from pathlib import Path
import paramiko
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "homes" / "ara"))
from finance.config import cfg

HOST = "bnu-proxmox"
user = "root"
key = Path.home() / ".ssh" / "id_ed25519"
cli = paramiko.SSHClient(); cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    cli.connect(HOST, username=user, key_filename=str(key), timeout=20, banner_timeout=30)
    print("connected with key", flush=True)
except Exception as e:
    print("key auth failed:", e, "-> trying password", flush=True)
    cli.connect(HOST, username=cfg("PROXMOX_LOGIN", "root"), password=cfg("PROXMOX_PW"), timeout=20, banner_timeout=30, allow_agent=False, look_for_keys=False)
    print("connected with password", flush=True)
tr = cli.get_transport(); tr.set_keepalive(30)

class Handler(socketserver.BaseRequestHandler):
    remote = None
    def handle(self):
        try:
            chan = tr.open_channel("direct-tcpip", self.remote, self.request.getpeername())
        except Exception as e:
            print("channel fail", self.remote, e, flush=True); return
        if chan is None: return
        try:
            while True:
                r, _, _ = select.select([self.request, chan], [], [], 60)
                if self.request in r:
                    d = self.request.recv(65536)
                    if not d: break
                    chan.sendall(d)
                if chan in r:
                    d = chan.recv(65536)
                    if not d: break
                    self.request.sendall(d)
        except Exception:
            pass
        finally:
            chan.close(); self.request.close()

class Srv(socketserver.ThreadingTCPServer):
    daemon_threads = True; allow_reuse_address = True

servers = []
for lport, rport in ((18788, 8788), (13000, 3000)):
    H = type(f"H{lport}", (Handler,), {"remote": ("10.1.1.126", rport)})
    s = Srv(("127.0.0.1", lport), H); servers.append(s)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"forwarding 127.0.0.1:{lport} -> 10.1.1.126:{rport}", flush=True)
while True:
    time.sleep(30)
    if not tr.is_active():
        print("transport died", flush=True); os._exit(1)
