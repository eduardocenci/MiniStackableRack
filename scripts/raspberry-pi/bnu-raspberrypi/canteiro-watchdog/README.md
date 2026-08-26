# canteiro-watchdog — aviso de queda do canteiro no WhatsApp (bnu-raspberrypi)

Vigia o canteiro da obra (ARA) e avisa o grupo **"Casa Céu Azul"**
(`120363410965618335@g.us`) via WAHA quando ele cai e quando volta:

- **Queda** (3 falhas seguidas ≈ 3 min, filtra soluços): envia a **última
  imagem da câmera antes da queda** com a legenda "📡 *Canteiro fora do ar*
  desde HH:MM — Starlink ou energia do barracão".
- **Volta** (primeiro sucesso): "✅ *Canteiro de volta ao ar* — ficou ~N min
  offline (HH:MM–HH:MM)".

## Como o "último frame" existe

No momento da queda a câmera já está inalcançável — então o watchdog
**cacheia um frame por minuto enquanto está no ar** (`/api/frame.jpeg` do
go2rtc local, que compartilha o produtor da tela de parede: custo ~zero) em
`/var/lib/canteiro-watchdog/lastframe.jpg`, e é esse cache que vai no alerta.

## Detecção

TCP connect em `100.66.255.82:8554` (tailnet IP do ara-raspberrypi — o
MagicDNS não importa aqui; testa Pi + mediamtx de uma vez). Horários das
mensagens em `America/Sao_Paulo` (o relógio deste Pi está em BST).

## Por que não é uma automação no Home Assistant

Sensores de rede no HA (`command_line`/`ping`) exigem editar
`configuration.yaml` e **reiniciar o HA da casa** — e o alerta continuaria
dependendo de WAHA do mesmo jeito. Este systemd timer entrega o mesmo
resultado sem tocar no HA. (Se um dia migrar: o script é a especificação.)

| Arquivo | Cópia viva |
|---|---|
| [`canteiro-watchdog.py`](canteiro-watchdog.py) | `/usr/local/bin/canteiro-watchdog.py` |
| [`canteiro-watchdog.service`](canteiro-watchdog.service) | `/etc/systemd/system/` (oneshot, `User=eduardocenci`, `StateDirectory`) |
| [`canteiro-watchdog.timer`](canteiro-watchdog.timer) | `/etc/systemd/system/` (a cada 60 s) |
| credenciais | `/etc/canteiro-watchdog.env` (600 root — WAHA_URL/KEY/SESSION, GROUP_JID, ARA_HOST/PORT, FAILS_TO_ALERT; espelho do `.env` `BNU_WAHA_*`) |

## Operar

```bash
python scripts/devtool.py run bnu-raspberrypi "systemctl list-timers canteiro-watchdog.timer --no-pager"
python scripts/devtool.py run bnu-raspberrypi "cat /var/lib/canteiro-watchdog/state.json"
# teste de alerta (usa o grupo SmokeTests para não incomodar a família):
python scripts/devtool.py run bnu-raspberrypi "sudo systemctl stop canteiro-watchdog.timer"  # pausar
```

Teste de mensagem: `canteiro-watchdog.py --test-alert <jid>` (com o env
carregado) — validado 2026-08-26 no grupo Casa SmokeTests (sendImage HTTP
201; o WAHA da frota envia mídia).
