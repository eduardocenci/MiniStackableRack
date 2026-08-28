# canteiro-sunset-compare — pôr do sol de ontem vs hoje (WhatsApp)

Toda **segunda–sexta às 20:10 America/Sao_Paulo** (dias de trabalho —
decisão Eduardo 27/08/2026; o script ainda pula sáb/dom se o `Persistent`
recuperar um disparo em fim de semana) este systemd timer no
bnu-raspberrypi baixa do Google Drive as fotos de
`CeuAzul/Timelapse/posicao1/por-do-sol/` de ontem e de hoje (produzidas
pelo timelapse do ara Pi, upload às 20:00 — ver
[`../../ara-raspberrypi/timelapse/`](../../ara-raspberrypi/timelapse/)),
empilha verticalmente — **ontem no topo, hoje embaixo**, largura 1600 px,
ffmpeg `vstack` — e manda no grupo WhatsApp via WAHA `sendImage` com a
legenda `🌇 Obra ARA — Dia de Trabalho (DD/MM)`
(funciona neste Core build; mesmo padrão do
[`../canteiro-watchdog/`](../canteiro-watchdog/)). A câmera queima
data/hora em cada frame, então a montagem dispensa legenda por imagem.

Pedido Eduardo 27/08/2026. Roda em bnu (e não no ara Pi) porque o WAHA
(LXC 101, `10.1.1.126`) é LAN-only de bnu; o Drive é o ponto de encontro.

## Peculiaridades que valem saber

- **O relógio deste Pi é Europe/London** — o fuso correto vem do
  `OnCalendar=... America/Sao_Paulo` e o script recalcula "hoje/ontem"
  com `ZoneInfo("America/Sao_Paulo")`. Nunca usar a data local do Pi.
- A foto de hoje pode ainda estar subindo às 20:10 (Starlink lenta): o
  script tenta de novo a cada 3 min por até ~25 min; se faltar foto,
  manda aviso de falha no mesmo chat em vez de silêncio.
- Última montagem enviada fica em `/tmp/ultima-comparacao.jpg` (inspeção).

## Install (as deployed 2026-08-27)

```
canteiro-sunset-compare.py      → /usr/local/bin/  (755)
canteiro-sunset-compare.service → /etc/systemd/system/
canteiro-sunset-compare.timer   → /etc/systemd/system/  (enable --now)
canteiro-sunset-compare.env.example → valores em /etc/canteiro-sunset-compare.env (600)
```

`WAHA_*`, `GROUP_JID`, `TEST_JID`: mesmos valores de
`/etc/canteiro-presenca.env`. `RCLONE_REMOTE=ceuazul:Timelapse` usa o
`rclone.conf` de `~eduardocenci` (token da mesma conta Google do upload
do ara Pi; `RCLONE_CONFIG` no env aponta para ele para o teste como root
funcionar).

## Teste sem pingar a família

```
sudo bash -c 'set -a; . /etc/canteiro-sunset-compare.env; set +a; canteiro-sunset-compare.py --test'            # → TEST_JID (Casa SmokeTests)
sudo bash -c 'set -a; . /etc/canteiro-sunset-compare.env; set +a; canteiro-sunset-compare.py --test <chatId>'   # → qualquer chat
```

A mensagem sai prefixada com `[TESTE]`. Smoke test de 27/08/2026:
HTTP 201, montagem 26/08×27/08 no Casa SmokeTests.
