#!/usr/bin/env python3
"""starlink-names — auto-apelida devices do netoverview com os nomes do roteador Starlink.

A cada 5 min (supercronic, container `starlink-names`) pergunta ao roteador
Starlink quem está associado ao Wi-Fi (gRPC `wifi_get_clients` — os mesmos nomes
que o app Starlink mostra: hostname DHCP ou given name) e, para cada cliente que
o netoverview já viu mas ainda NÃO tem apelido, grava o nome do roteador como
nickname (POST /api/nickname). Nunca sobrescreve apelido existente —
renomeações manuais na UI do netoverview sempre vencem.

Efeito: celulares novos na rede do canteiro ganham nome sozinhos minutos
depois de conectar; o relatório canteiro-presenca (bnu Pi) e o gate de
presença do frigate_whatsapp (bnu HA) passam a tratá-los como "conhecidos".

## MAC mascarado (correção 31/08/2026)

O firmware do roteador (apiVersion 131) devolve o `macAddress` de toda entrada
`role: CLIENT` **redigido até o OUI** — `54:ba:d9:XX:XX:XX` para a câmera, cujo
MAC real é `54:ba:d9:bd:34:e3`. Só a entrada `Controller` (o próprio roteador)
e o `upstreamMacAddress` vêm completos. A versão original casava direto por
`by_mac[macAddress]` e portanto **nunca apelidou nada** — 0 `nicknamed` em todo
o histórico desde 26/08/2026.

Resolução do MAC real, em ordem:
  1. MAC do roteador, se não vier mascarado (cobre `Controller` e um eventual
     firmware futuro que pare de redigir);
  2. link-local EUI-64 em `ipv6Addresses` — `fe80::56ba:d9ff:febd:34e3` volta a
     `54:ba:d9:bd:34:e3` (flip do bit U/L, tira o `ff:fe` do meio). Não existe
     quando o cliente usa link-local stable-privacy (RFC 7217);
  3. `ipAddress` do cliente casado com o `ip` do netoverview.
Nos casos 2 e 3 o OUI mascarado do roteador precisa bater com o OUI do device
achado — um lease DHCP velho não cola nome errado num aparelho (e nome errado
gruda: o script nunca sobrescreve).

Requer: grpcurl (baked no image; release oficial fullstorydev, linux_arm64).
Uso: `--dry-run` mostra a resolução de cada cliente sem gravar nada.
"""
import ipaddress
import json
import subprocess
import sys
import urllib.request

ROUTER = "192.168.1.1:9000"
NTO    = "http://127.0.0.1:5000"
SKIP_NAMES = {"", "unknown", "controller"}  # Controller = o próprio roteador
MASK = "xx"  # octeto redigido pelo firmware


def router_clients():
    out = subprocess.run(
        ["grpcurl", "-plaintext", "-max-time", "10",
         "-d", '{"wifi_get_clients":{}}',
         ROUTER, "SpaceX.API.Device.Device/Handle"],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)["wifiGetClients"]["clients"]


def nto_devices():
    with urllib.request.urlopen(f"{NTO}/api/devices", timeout=15) as r:
        return json.load(r)["devices"]


def set_nickname(mac, nickname):
    req = urllib.request.Request(
        f"{NTO}/api/nickname",
        data=json.dumps({"mac": mac, "nickname": nickname}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()


def oui(mac):
    """Os 3 primeiros octetos, minúsculos — ou None se não for um MAC."""
    parts = (mac or "").lower().split(":")
    return ":".join(parts[:3]) if len(parts) == 6 else None


def is_masked(mac):
    return MASK in (mac or "").lower().split(":")[3:]


def eui64_mac(client):
    """MAC real reconstruído do link-local EUI-64, se houver um."""
    for addr in client.get("ipv6Addresses") or []:
        if not addr.lower().startswith("fe80:"):
            continue
        try:
            iid = ipaddress.IPv6Address(addr.split("%")[0]).packed[8:]
        except ValueError:
            continue
        if iid[3:5] != b"\xff\xfe":
            continue  # stable-privacy (RFC 7217) — não carrega o MAC
        octets = bytes([iid[0] ^ 0x02]) + iid[1:3] + iid[5:]
        return ":".join(f"{b:02x}" for b in octets)
    return None


def resolve(client, by_mac, by_ip):
    """(device, via) do netoverview para este cliente do roteador, ou (None, motivo)."""
    router_mac = (client.get("macAddress") or "").lower()
    want_oui = oui(router_mac)

    if router_mac and not is_masked(router_mac) and router_mac in by_mac:
        return by_mac[router_mac], "mac"

    derived = eui64_mac(client)
    if derived and derived in by_mac:
        if want_oui and oui(derived) != want_oui:
            return None, "eui64-oui-mismatch"
        return by_mac[derived], "eui64"

    dev = by_ip.get(client.get("ipAddress") or "")
    if dev:
        if not dev.get("mac"):
            # visto pelo netoverview, mas sem MAC (o próprio Pi) — /api/nickname
            # é chaveado por MAC, então não há como apelidar
            return None, "no-mac-in-netoverview"
        if want_oui and oui(dev["mac"]) != want_oui:
            return None, "ip-oui-mismatch"
        return dev, "ip"

    return None, "unseen-by-netoverview"


def main():
    dry = "--dry-run" in sys.argv
    devices = nto_devices()
    by_mac = {d["mac"].lower(): d for d in devices if d.get("mac")}
    by_ip  = {d["ip"]: d for d in devices if d.get("ip")}

    changed = skipped_noname = 0
    for c in router_clients():
        name = (c.get("name") or "").strip()
        if name.lower() in SKIP_NAMES:
            if (c.get("role") or "") == "CLIENT":
                skipped_noname += 1
                if dry:
                    print(f"  no-name  {c.get('macAddress')} ip={c.get('ipAddress')}"
                          f" name={name!r}")
            continue

        dev, via = resolve(c, by_mac, by_ip)
        if dev is None:
            if dry:
                print(f"  unmatched {c.get('macAddress')} ip={c.get('ipAddress')}"
                      f" name={name!r} ({via})")
            continue
        if dev.get("nickname"):
            if dry:
                print(f"  has-nick {dev['mac']} name={name!r}"
                      f" nickname={dev['nickname']!r} (via {via})")
            continue

        if dry:
            print(f"  WOULD SET {dev['mac']} -> {name!r} (via {via})")
        else:
            set_nickname(dev["mac"], name)
            print(f"nicknamed {dev['mac']} -> {name!r} (via {via})")
        changed += 1

    verb = "would set" if dry else "set"
    print(f"done: {changed} nickname(s) {verb}"
          f"{f'; {skipped_noname} client(s) with no usable name' if skipped_noname else ''}")


if __name__ == "__main__":
    main()
