# bnu-raspberrypi — Tailscale exit node

`bnu-raspberrypi` advertises itself as a Tailscale **exit node** (since
2026-10-18): any tailnet client can send all its internet traffic out
through the bnu WAN (EdgeRouter `10.1.1.2`), e.g. to appear in Brazil while
travelling. It is the fleet's only Linux exit node; `mia-homeassistant` (HA
Tailscale add-on) is the other one.

## What `setup.sh` does

| Step | Where | Why |
|---|---|---|
| `net.ipv4.ip_forward = 1`, `net.ipv6.conf.all.forwarding = 1` | `/etc/sysctl.d/99-tailscale.conf` | IPv4 was already on (Docker); IPv6 was off |
| `ethtool -K eth0 rx-udp-gro-forwarding on rx-gro-list off` | `/etc/NetworkManager/dispatcher.d/50-tailscale-gro` (re-applied on every `eth0` up) | Tailscale's recommended UDP throughput setting for Linux exit nodes |
| `tailscale set --advertise-exit-node` | tailscaled prefs | `set`, not `up`, so no other pref is reset |

Re-run it after an SD rebuild:

```bash
B=$(git cat-file -p HEAD:scripts/raspberry-pi/bnu-raspberrypi/tailscale-exit-node/setup.sh | base64 -w0)
python scripts/devtool.py run bnu-raspberrypi "echo $B | base64 -d > /tmp/s.sh && bash /tmp/s.sh; rm -f /tmp/s.sh"
```

(`git cat-file` ships the LF blob — the working tree is CRLF here, see
`REMOTE_ACCESS.md` → Raspberry Pi row.)

**Then approve it once** in the admin console: Machines → `bnu-raspberrypi`
→ ⋯ → *Edit route settings* → tick **Use as exit node**. There is no
Tailscale API key in `.env` and the tailnet policy did not auto-approve it
(2026-10-18), so advertising alone does nothing. Approved = `"0.0.0.0/0"`
and `"::/0"` appear in the node's `AllowedIPs` (`tailscale status --json` →
`Self`), and `tailscale exit-node list` on any client lists it.

The firewall needs nothing: Docker sets the iptables `FORWARD` policy to
`DROP`, but tailscaled's `ts-forward` jump is the first rule of that same
chain (iptables-nft, one `ip filter` table) and accepts tailnet traffic
before Docker's chains see it.

## Using it

```bash
tailscale set --exit-node=bnu-raspberrypi --exit-node-allow-lan-access=true
tailscale set --exit-node=          # stop
```

Phones / Windows / macOS: Tailscale app → *Exit node* → `bnu-raspberrypi`.
Traffic to other tailnet nodes is unaffected either way.

Capacity: every byte crosses the bnu WAN twice (internet → Pi on the
downlink, Pi → client on the uplink), so throughput is capped by the bnu
upload speed, not by the Pi 4.

IPv6: the bnu WAN is IPv4-only (2026-10-18), so IPv6 sent to the exit node
goes nowhere and clients fall back to IPv4 (happy eyeballs). Forwarding is
enabled for v6 anyway so it starts working if the ISP ever hands out a
prefix.
