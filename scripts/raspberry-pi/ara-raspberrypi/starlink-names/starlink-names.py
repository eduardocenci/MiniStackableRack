#!/usr/bin/env python3
"""starlink-names — auto-apelida devices do netoverview com os nomes do roteador Starlink.

A cada 5 min (systemd timer) pergunta ao roteador Starlink quem está
associado ao Wi-Fi (gRPC `wifi_get_clients` — os mesmos nomes que o app
Starlink mostra: hostname DHCP ou given name) e, para cada cliente que o
netoverview já viu mas ainda NÃO tem apelido, grava o nome do roteador como
nickname (POST /api/nickname). Nunca sobrescreve apelido existente —
renomeações manuais na UI do netoverview sempre vencem.

Efeito: celulares novos na rede do canteiro ganham nome sozinhos minutos
depois de conectar; o relatório canteiro-presenca (bnu Pi) e o gate de
presença do frigate_whatsapp (bnu HA) passam a tratá-los como "conhecidos".

Requer: grpcurl em /usr/local/bin (instalado 2026-08-26; release oficial
fullstorydev/grpcurl, linux_arm64).
"""
import json
import subprocess
import urllib.request

ROUTER = "192.168.1.1:9000"
NTO    = "http://127.0.0.1:5000"
SKIP_NAMES = {"", "unknown", "controller"}  # Controller = o próprio roteador


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


def main():
    by_mac = {(d.get("mac") or "").lower(): d for d in nto_devices() if d.get("mac")}
    changed = 0
    for c in router_clients():
        mac  = (c.get("macAddress") or "").lower()
        name = (c.get("name") or "").strip()
        if not mac or name.lower() in SKIP_NAMES:
            continue
        dev = by_mac.get(mac)
        if dev is None or dev.get("nickname"):
            continue
        set_nickname(mac, name)
        print(f"nicknamed {mac} -> {name!r}")
        changed += 1
    print(f"done: {changed} nickname(s) set")


if __name__ == "__main__":
    main()
