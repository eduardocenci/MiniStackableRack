# starlink-names — router names → netoverview nicknames (auto)

Every 5 min, asks the Starlink router who is associated to the Wi-Fi
(local gRPC `192.168.1.1:9000`, `SpaceX.API.Device.Device/Handle` with
`{"wifi_get_clients":{}}` — the same names the Starlink app shows) and
nicknames in netoverview any device that has none yet. Manual renames in
the netoverview UI are never overwritten.

Why: phones on the canteiro LAN use randomized (per-SSID-stable) MACs and
answer no reverse DNS/mDNS, so netoverview shows them as bare IPs. The
router knows their DHCP hostnames ("Galaxy-A54-5G", "A23-de-Edy"…). With
this sync, a phone gets its name minutes after it first connects, and the
daily presence report (`bnu-raspberrypi/canteiro-presenca/`) shows real
names instead of MAC stubs. Names are display-only: the Frigate WhatsApp
gate (bnu HA `frigate_whatsapp.py`) suppresses on ANY non-fixed device
online, nicknamed or not — whoever has the Wi-Fi credentials is assumed
allowed at the obra (decisão Eduardo 26/08/2026).

Notes:
- `wifi_get_clients` lists only currently-associated clients (the app's
  full device list with disconnected entries lives in the Starlink cloud,
  not on the router — probed 2026-08-26: no local roster RPC).
- The `Controller` entry (the router itself) is skipped; router and camera
  were hand-nicknamed ("Roteador Starlink", "Câmera do canteiro (iM9)").
- **MAC de cliente vem mascarado** (achado 2026-08-31): o firmware
  (apiVersion 131) redige o `macAddress` de toda entrada `role: CLIENT` até
  o OUI — `54:ba:d9:XX:XX:XX` para a câmera, cujo MAC real é
  `54:ba:d9:bd:34:e3`. Só `Controller` e `upstreamMacAddress` vêm inteiros.
  A primeira versão casava direto por `by_mac[macAddress]` e por isso
  **nunca apelidou nada** entre 26/08 e 31/08/2026 (0 `nicknamed` no
  journal e em 390 iterações do container; os 2 apelidos existentes são
  manuais). Desde 31/08 o MAC real é resolvido em 3 tentativas — MAC não
  mascarado → link-local EUI-64 em `ipv6Addresses` → `ipAddress` casado com
  o `ip` do netoverview — e as duas últimas exigem que o OUI mascarado
  bata, para um lease DHCP velho não colar nome errado (nome errado gruda:
  o script nunca sobrescreve).
- Um celular só ganha nome se **reportar** um ao roteador. MAC randomizado
  não impede (o netoverview vê o mesmo MAC randomizado por ARP), mas
  aparelho que suprime o hostname DHCP chega como `unknown`/vazio e cai no
  `SKIP_NAMES` — nenhum casamento de MAC resolve isso.
- `grpcurl` v1.9.1 (official fullstorydev release, linux_arm64) is baked
  into the container image (a host copy from 2026-08-26 remains at
  `/usr/local/bin/grpcurl`).

Install: Docker container `starlink-names` since 2026-08-29
([`../docker/starlink-names/`](../docker/starlink-names/) — own image,
deliberately separate from the camera stack; supercronic every 5 min; the
old service+timer stay on the Pi disabled, one wave, as rollback). Test:
`docker exec starlink-names python3 /app/starlink-names.py` (prints
`done: N nickname(s) set`; never overwrites, so re-runs are safe).
Diagnóstico: `docker exec starlink-names python3 /app/starlink-names.py
--dry-run` — mostra, por cliente associado, como o MAC foi resolvido
(`via eui64`/`ip`/`mac`) ou por que não foi (`no-name`, `ip-oui-mismatch`,
`no-mac-in-netoverview`, `unseen-by-netoverview`), sem gravar nada.
