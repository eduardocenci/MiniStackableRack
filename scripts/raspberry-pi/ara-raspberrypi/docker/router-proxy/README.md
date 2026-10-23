# router-proxy — Starlink router gRPC → tailnet (Wi-Fi client list)

socat container bridging the Starlink **router** local gRPC API
(`192.168.1.1:9000`, plaintext gRPC, no auth, server reflection on) to the
tailnet as `ara-raspberrypi2:9000`. Twin of [`../starlink-proxy/`](../starlink-proxy/),
which relays the **dish** (`:9200`) — same `Device/Handle` service, different
device, different RPCs.

Consumer: **canteiro-vigia** on bnu-raspberrypi
(`scripts/raspberry-pi/bnu-raspberrypi/canteiro-vigia/`, env `ROUTER_GRPC=
ara-raspberrypi2:9000`). One `wifi_get_clients` call lists every associated
Wi-Fi client with `name`, `ipAddress`, a MAC masked to the OUI
(`54:ba:d9:XX:XX:XX` — firmware apiVersion ≥ 131, see
[`../../starlink-names/`](../../starlink-names/)) and **`associatedTimeS`** —
seconds since the client associated. `now − associatedTimeS` is the exact
instant a phone joined the canteiro network, which is how the weekend watch
decides "someone with the Wi-Fi password arrived" (🟢) vs "nobody joined in
the minute after the detection" (🔴) without waiting for netoverview's 5-min
ARP scan.

Exposure: nothing new — any client of the house LAN already reaches the
router API directly; the relay only adds the tailnet (our own devices).

Test (from the Pi — a grpcurl host copy sits at `/usr/local/bin/grpcurl`):

```bash
grpcurl -plaintext -d '{"wifi_get_clients":{}}' localhost:9000 SpaceX.API.Device.Device/Handle | grep -E '"name"|associatedTimeS'
```

Expect the camera (`iM9-M-9411`), the Pi and the phones on site, each with an
`associatedTimeS`, within ~1 s. Registered as `ara_rtproxy` in
`globalnet/architecture.yaml` (audited by `make fleet`); started by the
site-gate with the other stacks (`STACKS` in
[`../../site-gate/ara-site-gate.sh`](../../site-gate/ara-site-gate.sh)).

Deploy: copy this folder to `~/router-proxy/` on the Pi and
`docker compose up -d` there.
