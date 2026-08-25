# dvrip-bridge — camera port 37777 on the tailnet (ara-raspberrypi)

TCP forward (socat) of the iM9 camera's **Intelbras-1 / Dahua DVRIP** port
onto the tailnet, so Intelbras desktop software (S.I.M. Next etc.) running
on any tailnet machine can talk the private protocol to the LAN-only camera:

```
S.I.M. Next (PC, tailnet) ──▶ ara-raspberrypi:37777 ──socat──▶ 192.168.1.56:37777
```

In the software, add the camera by IP using the **Pi's tailnet address**
(`ara-raspberrypi` / `100.66.255.82`), port `37777`, protocol
**Intelbras-1**, user `admin`, password = the camera's Device Password
(`.env` `ARA_CANTEIRO_CAM_KEY`).

| File | Live copy |
|---|---|
| [`dvrip-bridge.service`](dvrip-bridge.service) | `/etc/systemd/system/dvrip-bridge.service` |

Notes: TCP only (37777). Some flows also use UDP 37778 — not forwarded;
add a second socat line if a tool demands it. The forward is protocol-blind
(no credentials involved on the Pi).
