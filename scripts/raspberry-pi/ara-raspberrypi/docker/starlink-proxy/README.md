# starlink-proxy — dish gRPC → tailnet (live WAN throughput + watts)

socat container bridging the Starlink **dish** local gRPC API
(`192.168.100.1:9200`, plaintext gRPC, no auth, server reflection on) to
the tailnet as `ara-raspberrypi:9200`.

Consumer: **globalnet** on bnu-raspberrypi (`ARA_STARLINK_GRPC=
ara-raspberrypi:9200` in `~/globalnet/.env`). One `get_history` call gives
900 s of 1 Hz ring buffers — downlink/uplink throughput and `powerIn` —
which the dashboard renders as the ARA WAN card's "live ▼ ▲ Mbps" line and
the `ara_dish` ⚡ W badge (globalnet `docs/runbooks/monitoring.md`).

Note this is the **dish**, not the router: `starlink-names/` talks to the
router (`192.168.1.1:9000`) for Wi-Fi client names; throughput and power
telemetry live only on the dish.

Test (from the Pi — a grpcurl host copy sits at `/usr/local/bin/grpcurl`):

```bash
grpcurl -plaintext -d '{"get_status":{}}' localhost:9200 SpaceX.API.Device.Device/Handle
```

Expect `downlinkThroughputBps`/`uplinkThroughputBps` in the reply within
~1 s. Registered as `ara_slproxy` in `globalnet/architecture.yaml`
(audited by `make fleet`).
