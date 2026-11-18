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
go2rtc local: custo ~zero) em `/var/lib/canteiro-watchdog/lastframe.jpg`, e
é esse cache que vai no alerta. Desde 14/11/2026 o frame é o **mosaico**
`[lente fixa | lente PT]` — `GO2RTC_FRAME`, agora configurável pelo env,
tem por padrão `http://127.0.0.1:1984/api/frame.jpeg?src=canteiro_mosaico`
(H.264 2560×720; o endpoint com `src=canteiro`, HEVC, vinha respondendo
HTTP 500).

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
considera o /live travado se a playlist (`HLS_URL`, por padrão
`http://127.0.0.1:8888/mosaico/index.m3u8?cookieCheck=1` desde 14/11/2026 —
o mosaico é o que o /live toca; antes era `/canteiro/`) não devolver
`#EXTM3U` **ou** se o log do canteiro-hls tiver ≥3 linhas ERR de crash nos
últimos 90 s (lidas pelo socket do Docker — a janela nunca olha para trás
do último heal). Dois ticks travados seguidos (~2 min) → reinicia os dois
containers pelo socket (`/var/run/docker.sock` montado no compose,
`group_add: 984` = grupo `docker` do host). No máximo 2 reinícios por
episódio; se não resolver, escala e para. Interrupção colateral: o Frigate
perde ~5 s de gravação na reconexão (aceito, decisão Eduardo 31/08).

**Causa raiz eliminada em 29/10/2026** — o heal fica só como rede de
segurança. O salto de timestamp nascia nos EOFs que a câmera emitia no
início das excursões PTZ do timelapse (que deixaram de existir em
14/11/2026 — a câmera não é mais movida; 1–3 pares `🔧 Auto-heal` /
`✅ recuperado` por sequência solar, quase todo dia): o relay reabria a
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

**Mosaico (14/11/2026):** a playlist vigiada depende agora também do
`canteiro-mosaico.service` no LXC 105 do Frigate (publica o caminho
`mosaico` no canteiro-hls). Se ele parar, a playlist some e reiniciar
go2rtc + canteiro-hls não a traz de volta — o remédio aí é o serviço
(`python scripts/devtool.py run bnu-proxmox "pct exec 105 -- systemctl
status canteiro-mosaico"`; ver
[`../../../proxmox/frigate/bnu-frigate/README.md`](../../../proxmox/frigate/bnu-frigate/README.md)).

Notas de heal/escalação vão a `HEAL_JID` (**Casa SmokeTests** — a família
não vê nada disso; vazio = só stdout). Tunáveis no env: `GO2RTC_FRAME`, `HLS_URL`,
`HLS_FAILS_TO_HEAL` (2), `MAX_HEAL_ATTEMPTS` (2), `HEAL_CONTAINERS`
(`go2rtc,canteiro-hls`), `DOCKER_SOCK`.

**Não aumentar `HLS_FAILS_TO_HEAL`** (testado em 17/11/2026, 5 ticks por ~45 min,
revertido). Quando o feed do canteiro tem um buraco, o `canteiro-hls` derruba o
mosaico e o compositor (`canteiro-mosaico`, LXC do Frigate) passa a reiniciar a
cada ~70 s sem conseguir publicar — na queda das 09:34 foram 4 ciclos, e o mosaico
só voltou 16 s depois de o heal reiniciar go2rtc + canteiro-hls. Com 5 ticks a
tela ficou ~5 min fora; com 2, as quedas da mesma manhã duraram de 1 a 4,5 min. O heal é
o remédio também para o mosaico, não um agravante. O journal do compositor na
queda das 10:33 (17/11/2026) mostra por que ele não publica nesses ciclos: o
ffmpeg sobe, não recebe nenhum quadro das entradas (`canteiro_fixa`/`canteiro`
no go2rtc do Frigate) — "No filtered frames for output stream", 1 s de CPU em
65 s — e o próprio script o encerra por "0 of 2 publisher connections". Ou seja,
o mosaico depende de as duas lentes voltarem a entregar quadros; segurar o último
quadro (ou o painel sozinho) durante o buraco seria uma mudança de desenho do
compositor, não feita.

**Essa mudança de desenho foi feita em 18/11/2026** (`canteiro-mosaico.py`: um
alimentador por lente, uma bomba de 15 fps que repete o último quadro e um
codificador sem entrada de rede — ver "holds the last frame" em
[`../../../proxmox/frigate/bnu-frigate/README.md`](../../../proxmox/frigate/bnu-frigate/README.md)).
Com ela no ar, um buraco no feed deixa a metade parada e marcada "SEM SINAL ·
último quadro HH:MM:SS" e o mosaico segue sendo publicado: a playlist vigiada não
some, e o heal deixa de ser o remédio do mosaico (continua sendo o do go2rtc e do
canteiro-hls). Os parágrafos acima descrevem o compositor de processo único que
rodou até essa troca.

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
