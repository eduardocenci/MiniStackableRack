# canteiro-sunset-compare — "Dia de Trabalho": grade do pôr do sol (WhatsApp)

Toda **segunda–sexta às 20:10 America/Sao_Paulo** (dias de trabalho —
decisão Eduardo 27/08/2026; o script ainda pula sáb/dom por garantia) o
container `canteiro-sunset-compare` no bnu-raspberrypi (supercronic — ver
[`../docker/canteiro-jobs/`](../docker/canteiro-jobs/); systemd timer até
2026-08-29) baixa do Google Drive as fotos do pôr do sol nas TRÊS posições
da lente PT (timelapse do ara Pi, upload às 20:00 — ver
[`../../ara-raspberrypi/timelapse/`](../../ara-raspberrypi/timelapse/)) e
monta a **grade 2×3** (layout Eduardo 31/08/2026):

|  | esquerda | centro | direita |
|---|---|---|---|
| **linha 1 — dia anterior** | posicao3 | posicao1 | posicao2 |
| **linha 2 — dia corrente** | posicao3 | posicao1 | posicao2 |

800 px por célula (2400×900, ffmpeg hstack+vstack). Dia anterior = ontem;
**segunda compara com sexta** (exceção única 31/08/2026: usou domingo
30/08, primeiro dia com as três posições). Envio no grupo via WAHA
`sendImage` com a legenda `🌇 Dia de Trabalho (DD/MM)` (funciona neste
Core build; mesmo padrão do
[`../canteiro-watchdog/`](../canteiro-watchdog/)). A câmera queima
data/hora em cada frame, então a grade dispensa legenda por célula.

Pedido Eduardo 27/08/2026. Roda em bnu (e não no ara Pi) porque o WAHA
(LXC 101, `10.1.1.126`) é LAN-only de bnu; o Drive é o ponto de encontro.

## Peculiaridades que valem saber

- **O relógio deste Pi é Europe/London** — o fuso correto vem do
  `TZ=America/Sao_Paulo` do container (supercronic agenda nesse fuso) e o
  script recalcula "hoje/ontem" com `ZoneInfo("America/Sao_Paulo")`.
  Nunca usar a data local do Pi.
- A foto de hoje pode ainda estar subindo às 20:10 (Starlink lenta): o
  script tenta de novo a cada 3 min por até ~25 min; se faltar foto,
  manda aviso de falha no mesmo chat em vez de silêncio.
- Última montagem enviada fica em `/tmp/ultima-comparacao.jpg` **dentro do
  container** (inspeção: `docker exec canteiro-sunset-compare ls -la /tmp`;
  some quando o container é recriado).
- Cada grade também é **arquivada no Drive** em
  `Timelapse/DiaDeTrabalho/YYYY-MM-DD.jpg` — topo do Timelapse (série
  diária pronta para consulta/IA, além do envio no WhatsApp; movida de
  `posicao1/DiaDeTrabalho/` em 31/08/2026, arquivos antigos migrados).
- **Destino de produção ATIVO desde 27/08/2026** (comando do Eduardo):
  grupo **Cenci Céu Azul Casa-Hangar** (`120363402090094156@g.us`). O JID
  do Casa SmokeTests fica comentado no env como rollback/staging.

## Install (container desde 2026-08-29)

Empacotamento, deploy, env e rollback em
[`../docker/canteiro-jobs/`](../docker/canteiro-jobs/) — esta pasta é dona
só do script e do formato do env
([`canteiro-sunset-compare.env.example`](canteiro-sunset-compare.env.example),
valores vivos em `~/canteiro-jobs/env/canteiro-sunset-compare.env`, 600).

`WAHA_*`, `GROUP_JID`, `TEST_JID`: mesmos valores do env do presenca.
`RCLONE_REMOTE=ceuazul:Timelapse` usa o `rclone.conf` de `~eduardocenci`
**montado rw no container** (`/config/rclone`, uid 1000 — o refresh do
token não troca o dono do arquivo no host; token da mesma conta Google do
upload do ara Pi). Diferente do timer (`Persistent=true`), o supercronic
**não recupera** um disparo perdido com o Pi desligado.

## Teste sem pingar a família

```
docker exec canteiro-sunset-compare python3 /app/canteiro-sunset-compare.py --test            # → TEST_JID (Casa SmokeTests)
docker exec canteiro-sunset-compare python3 /app/canteiro-sunset-compare.py --test <chatId>   # → qualquer chat
```

A mensagem sai prefixada com `[TESTE]`. Smoke tests: 27/08/2026 (systemd,
montagem 26/08×27/08) e 29/08/2026 (container: aviso de foto faltando +
montagem 27/08×28/08 manual via ffmpeg) — ambos HTTP 201 no Casa
SmokeTests.
