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
go2rtc local, cujo produtor o Frigate e o canteiro-hls mantêm ativo: custo
~zero) em `/var/lib/canteiro-watchdog/lastframe.jpg`, e é esse cache que
vai no alerta.

## Detecção

TCP connect em `100.66.255.82:8554` (tailnet IP do ara-raspberrypi — o
MagicDNS não importa aqui; testa Pi + mediamtx de uma vez). Horários das
mensagens em `America/Sao_Paulo` (o relógio deste Pi está em BST).

## Por que não é uma automação no Home Assistant

Sensores de rede no HA (`command_line`/`ping`) exigem editar
`configuration.yaml` e **reiniciar o HA da casa** — e o alerta continuaria
dependendo de WAHA do mesmo jeito. Este job entrega o mesmo resultado sem
tocar no HA. (Se um dia migrar: o script é a especificação.)

**Desde 2026-08-29 roda como container Docker** (`canteiro-watchdog`, loop
de 60 s) — empacotamento, deploy, teste e rollback em
[`../docker/canteiro-jobs/`](../docker/canteiro-jobs/). Até então era um
systemd timer; os unit files antigos seguem no Pi **desabilitados** como
rollback por uma onda, depois somem.

| Arquivo | Cópia viva |
|---|---|
| [`canteiro-watchdog.py`](canteiro-watchdog.py) | `~/canteiro-jobs/canteiro-watchdog.py` (COPY no build da imagem) |
| estado | `/var/lib/canteiro-watchdog/` (bind mount — o mesmo dir da era systemd) |
| credenciais | `~/canteiro-jobs/env/canteiro-watchdog.env` (600 — WAHA_URL/KEY/SESSION, GROUP_JID, ARA_HOST/PORT, FAILS_TO_ALERT; espelho do `.env` `BNU_WAHA_*`) |

## Operar

```bash
python scripts/devtool.py run bnu-raspberrypi "docker ps --filter name=canteiro-watchdog"
python scripts/devtool.py run bnu-raspberrypi "cat /var/lib/canteiro-watchdog/state.json"
python scripts/devtool.py run bnu-raspberrypi "docker compose -f ~/canteiro-jobs/compose.yml stop canteiro-watchdog"  # pausar
```

Teste de mensagem: `docker exec canteiro-watchdog python3
/app/canteiro-watchdog.py --test-alert <jid-do-SmokeTests>` — **sempre com
o jid explícito** (sem argumento o flag manda ao grupo real). Validado
2026-08-26 (systemd) e 2026-08-29 (container) no grupo Casa SmokeTests
(sendImage HTTP 201).
