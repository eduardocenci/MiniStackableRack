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

TCP connect em `100.65.218.33:8554` (tailnet IP do ara-raspberrypi2 — o
MagicDNS não importa aqui; testa Pi + mediamtx de uma vez). Horários das
mensagens em `America/Sao_Paulo` (o relógio deste Pi está em BST).

## Auto-heal do /live (2026-08-31)

O go2rtc sobrevive às reconexões do produtor (quedas da Starlink) mas o
restream sai com timestamps que o mediamtx do `canteiro-hls` não engole —
o muxer entra em crash-loop (`muxer error: sample timestamp is impossible
to handle`) e **nunca se recupera sozinho** (incidente 29→31/08/2026:
1658 crashes, `/live` morto 2 dias, Frigate normal o tempo todo — o ffmpeg
tolera os saltos, o muxer HLS não). Reiniciar só o canteiro-hls NÃO
destrava (provado 2x); o remédio é reiniciar **go2rtc e DEPOIS
canteiro-hls**.

O watchdog agora aplica esse remédio: a cada tick com o **relay em pé**,
considera o /live travado se a playlist
(`http://127.0.0.1:8888/canteiro/index.m3u8?cookieCheck=1`) não devolver
`#EXTM3U` **ou** se o log do canteiro-hls tiver ≥3 linhas ERR de crash nos
últimos 90 s (lidas pelo socket do Docker — a janela nunca olha para trás
do último heal). Dois ticks travados seguidos (~2 min) → reinicia os dois
containers pelo socket (`/var/run/docker.sock` montado no compose,
`group_add: 984` = grupo `docker` do host). No máximo 2 reinícios por
episódio; se não resolver, escala e para. Interrupção colateral: o Frigate
perde ~5 s de gravação na reconexão (aceito, decisão Eduardo 31/08).

**Causa raiz eliminada em 29/10/2026** — o heal fica só como rede de
segurança. O salto de timestamp nascia nos EOFs que a câmera emite no
início das excursões PTZ do timelapse (1–3 pares `🔧 Auto-heal` /
`✅ recuperado` por sequência solar, quase todo dia): o relay reabre a
câmera com outra base RTP e o go2rtc trocava o produtor por baixo da
sessão do canteiro-hls. Agora o `canteiro-hls` puxa o relay ele mesmo, com
`alwaysAvailable` (cartão "sem sinal" no buraco, linha do tempo contínua),
e o go2rtc lê dele em `127.0.0.1:8555` — ver
[`../docker/canteiro-hls/README.md`](../docker/canteiro-hls/README.md).
Efeito colateral aceito: com a câmera fora e o relay em pé, o frame que o
watchdog cacheia (`frame.jpeg` do go2rtc) pode ser o cartão em vez da
última imagem real — só aparece num alerta de queda se a Starlink cair
dentro do minuto seguinte.

**O contador de crashes depende de um log limpo.** Ele lê o log do
canteiro-hls pela API do Docker com `since=`, e essa leitura para na
primeira linha cheia de NUL que uma queda de energia ou reset deixa no
json log (`docker logs --tail` lê de trás para frente e não sofre). De
03/09 a 29/10/2026 o critério de crash ficou cego e os heals vieram só do
teste da playlist. O recreate do canteiro-hls em 29/10 começou um arquivo
limpo. Teste e detalhes em
[`../docker/canteiro-hls/README.md`](../docker/canteiro-hls/README.md)
(Gotchas).

Notas de heal/escalação vão a `HEAL_JID` (**Casa SmokeTests** — a família
não vê nada disso; vazio = só stdout). Tunáveis no env: `HLS_URL`,
`HLS_FAILS_TO_HEAL` (2), `MAX_HEAL_ATTEMPTS` (2), `HEAL_CONTAINERS`
(`go2rtc,canteiro-hls`), `DOCKER_SOCK`.

Heal manual (o remédio do runbook em um comando):

```bash
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-watchdog python3 /app/canteiro-watchdog.py --heal-now"
```

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
| credenciais | `~/canteiro-jobs/env/canteiro-watchdog.env` (600 — WAHA_URL/KEY/SESSION, GROUP_JID, ARA_HOST/PORT, FAILS_TO_ALERT, HEAL_JID; espelho do `.env` `BNU_WAHA_*` + `SMOKETESTS_WHATSAPP_GROUP_JID`) |

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
