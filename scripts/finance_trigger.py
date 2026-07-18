#!/usr/bin/env python3
"""PC trigger endpoint for waha-listener rules — Casa Financeiro.

Tiny stdlib HTTP server that the waha-listener container (bnu-proxmox LXC
101) POSTs to when a rule fires (e.g. Eduardo's "Casa, financeiro" wake word).
On a valid ping it spawns a headless Claude Code run of the finance-hangar
skill in this repo; the run itself pulls the pending job + messages from the
listener API. If this PC is off, nothing is lost — the pending job waits for
the 15-min `casa-financeiro` scheduled task (fallback sweeper).

Auth: X-Listener-Token header must equal BNU_WAHA_LISTENER_TOKEN (repo .env).
Single-flight: while a spawned run is still alive, further pings are answered
202 and NOT spawned again (the running skill drains the whole queue anyway).

Install (Task Scheduler, at log on):
  schtasks /Create /TN CasaFinanceiroTrigger /SC ONLOGON /RL LIMITED ^
    /TR "\"<pythonw.exe>\" \"<this file>\""

Log: gitignore/finance/trigger.log
"""
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOG = REPO / "gitignore" / "finance" / "trigger.log"

RULE_PROMPTS = {
    # rule name (from waha-listener rules.yaml) → slash command to run
    "finance": "/finance-hangar",
}


def env() -> dict:
    """KEY=VALUE pairs from the repo root .env (no external deps)."""
    out = {}
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


ENV = env()
TOKEN = ENV.get("BNU_WAHA_LISTENER_TOKEN", "")
PORT = int(ENV.get("ARA_FIN_PC_TRIGGER_PORT", "8799"))
CLAUDE = ENV.get("ARA_FIN_TRIGGER_CLAUDE", "claude")

_active: dict[str, subprocess.Popen] = {}


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def spawn(rule: str) -> str:
    """Start a headless Claude run for the rule; 'running' if one is active."""
    proc = _active.get(rule)
    if proc and proc.poll() is None:
        return "running"
    prompt = RULE_PROMPTS.get(rule)
    if not prompt:
        return "unknown-rule"
    logfile = (LOG.parent / f"run-{rule}-{time.strftime('%Y%m%d-%H%M%S')}.log").open(
        "w", encoding="utf-8")
    # No --permission-mode flag: the run inherits the repo's own
    # .claude/settings.local.json (the user's standing permission choice).
    _active[rule] = subprocess.Popen(
        [CLAUDE, "-p", prompt],
        cwd=str(REPO), stdout=logfile, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, shell=(os.name == "nt"),
    )
    return "spawned"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler API
        if not TOKEN or self.headers.get("X-Listener-Token") != TOKEN:
            self.send_response(401)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            job = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            job = {}
        rule = job.get("rule", "finance")
        result = spawn(rule)
        log(f"ping rule={rule} job={job.get('job_id', '?')} → {result}")
        self.send_response(202 if result == "running" else
                           200 if result == "spawned" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"result": result}).encode())

    def do_GET(self):  # noqa: N802 — health check
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *_args):  # silence default stderr chatter
        pass


def main() -> None:
    if not TOKEN:
        print("BNU_WAHA_LISTENER_TOKEN missing from .env — refusing to start",
              file=sys.stderr)
        sys.exit(1)
    log(f"listening on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
