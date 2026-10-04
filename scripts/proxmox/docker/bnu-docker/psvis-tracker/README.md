# psvis-tracker — cartões de voo do PS-VIS

Container no LXC 101 (`10.1.1.126`, porta **8790**) que envia as mensagens do
PS-VIS ao grupo WhatsApp **"Aeronave PS-VIS"** via WAHA (decisão Eduardo
2026-08-29 — o grupo da família não recebe mais alertas de voo): **um cartão
claro por etapa** (decisão Eduardo 2026-10-03) — decolagem, em voo (T+10) e
pouso (2 imagens), cada um com a legenda completa em texto. Ver
[Cartões](#cartões-decisão-eduardo-2026-10-03).

## Cartões (decisão Eduardo 2026-10-03)

PNG 1080×1350 (4:5, o formato que o WhatsApp mostra melhor), tema claro,
fonte Inter (`fonts-inter` no Dockerfile; sem ela, DejaVu Sans), renderizados
por `cards.py` (API orientada a objeto do matplotlib + lock — o tracker é
multi-thread). Opções A–E do redesign, todas adotadas:

| Etapa | Quando | Cartão |
|---|---|---|
| Decolagem | wheels-up | chips (hora, origem, meteo com ícone, **próxima atualização ~T+10**) + mapa da região de saída com o avião no rumo inicial + painel do METAR (ícone, estação, hora da observação, temperatura, nuvens, vento, visibilidade, METAR cru). **Sem palpite de destino** — nem no cartão nem na legenda, e sem "destino não informado" (decisão Eduardo 2026-10-03): para onde vai é assunto do cartão de T+10 |
| Em voo | T+10 | chips (altitude + razão de subida, velocidade, rumo com bússola, chegada estimada com **ícone do tempo no destino**) + mapa com o trecho voado colorido pela altitude, avião, perna tracejada até cada destino candidato com chip de ETA, voo anterior da rota em cinza + faixa "perfil até agora e relevo à frente" + linha do METAR do destino com ícone |
| Pouso 1/2 | ~2 min após o toque | chips (tempo de voo, **vs esperado** verde/âmbar, cruzeiro, distância) + mapa-herói com a rota colorida pela altitude, voo anterior em cinza e linha reta — leva a legenda completa |
| Pouso 2/2 | logo em seguida | chips de fases (subida, cruzeiro, descida, relevo máx.) + altitude sobre o **relevo real** por distância, com fases marcadas + **altitude e velocidade por minutos desde a decolagem, ao lado dos voos anteriores** da rota (mesma direção ou volta) e a linha do esperado |

Fontes: mapa = os tiles OSM que o `maptile` já cacheia em `/data/tiles`,
**dessaturados e clareados** (`_quiet`) — o CARTO Positron passou a responder
"API KEY REQUIRED" sem chave (2026-10-03), não usar; relevo = Copernicus DEM
90 m via Open-Meteo (`/v1/elevation`, sem chave, 100 pontos/chamada), cache em
`/data/terrain/<id>_*.json`, best-effort; METAR = aviationweather.gov, **do
horário descrito** (`date=`/`hours=`) quando ele já passou — reenvios e
simulações mostram o tempo daquele dia. **Ícones de tempo** desenhados em
vetor (`_wx_icon`: sol, sol entre nuvens, nuvem, chuva, trovoada, neblina,
névoa) a partir de `metar.condition()` — a mesma chave que escolhe o emoji da
legenda (☀️ ⛅ ☁️ 🌧️ ⛈️ 🌫️), então imagem e texto sempre concordam.

**Nunca perde mensagem**: cartão de pouso falhou → sai a imagem antiga
(gráfico + mapa) com a mesma legenda; cartão em voo falhou → mapa OSM antigo;
cartão de decolagem falhou 3× (20 s entre tentativas — logo após a decolagem
o FR24 pode ainda não servir o trail) → texto (`fallback_text` do HA ou
`_takeoff_text`). Só o fetch/render é repetido; o envio acontece uma vez.

**Decolagem em casa (BNU)**: a automação do HA chama `/report` com
`direction:"took_off", "announce": true` e o texto dela como `fallback_text`
— o tracker manda o cartão e agenda o T+10; tracker fora do ar → o HA manda o
texto ele mesmo (mesmo padrão do pouso). O live watch, ao ver a decolagem em
casa, só age se ninguém anunciou em `HOME_BACKUP_S` (150 s). Fora de casa o
live watch / watch da lista mandam o cartão. Todas as rotas disputam a mesma
marca `takeoff:<id>` com `_claim()` atômico — nunca dois anúncios.

## Fluxo do pouso (decisão Eduardo 2026-08-29)

1. No pouso, a automação `psvis_blumenau_flight_alert` no bnu-homeassistant
   (`scripts/proxmox/homeassistant/bnu-homeassistant/packages/flightradar_psvis.yaml`)
   **não manda texto** — chama `POST http://10.1.1.126:8790/report` com o
   `flight_id` do evento FR24 e um `fallback_text` (o texto enriquecido
   renderizado no HA). Decolagens também são delegadas (ver Cartões).
2. O serviço espera `INITIAL_DELAY_S` (o FR24 leva ~1–2 min para finalizar o
   track após o toque) e busca o **playback** do voo — o trail completo com
   altitude/velocidade ponto a ponto. Se o `flight_id` não vier, resolve o voo
   mais recente do PS-VIS com pouso real via `flight/list.json`.
   **Aeroportos completados pela lista** (2026-10-02): o playback às vezes vem
   sem `destination` (ou `origin`) que a entrada de `flight/list.json` já tem
   (41f01799 SOD→BNU saiu sem aeroporto de chegada). `_fill_airports` copia o
   lado inteiro da lista **só quando o playback não o tem** — nunca
   sobrescreve. Vale para o relatório e para a varredura; consulta à lista que
   falha não bloqueia o envio. Sem destino em nenhuma das fontes → fica
   desconhecido (sem inferência pelo último ponto — decisão Eduardo 2026-10-02).
3. Calcula: altitude de cruzeiro e velocidade de cruzeiro (**média no trecho
   contíguo estabilizado** a ≥95 % da altitude máxima), velocidade máxima,
   duração e distância da rota, e monta a legenda inteira a partir do playback.
   **Duração = wheels-up → wheels-down, nunca táxi** (2026-10-02): o ponto em
   solo vizinho só conta se estiver a ≤ `EDGE_S` (60 s) do primeiro/último
   ponto em voo — é a corrida de decolagem/pouso; senão vale a própria borda
   airborne. (4150ac1d SBCX→SSBL tinha o último ponto de solo 9 min antes da
   decolagem e foi gravado com 48,5 min em vez de 39,6 — banco recalculado.)
   **Comparação com o esperado**: linha `⏱️ *X min mais rápido/lento* que o
   esperado (média … em N voos)` contra a média da rota no flight log (mesma
   direção, senão a inversa, anotada; o próprio voo excluído). Diferença
   < 1 min → "No tempo esperado"; sem histórico na rota → linha omitida;
   `🏆 mais rápido já registrado` só com ≥2 voos na mesma direção.
4. Renderiza os cartões de pouso 1/2 e 2/2 e envia via WAHA `sendImage`
   (legenda no 1/2). Os PNGs são servidos em `/charts/<id>-1.png`,
   `<id>-2.png` (decolagem `<id>-takeoff.png`, em voo `<id>-enroute.png`)
   porque o WAHA Core busca imagens por URL — resolve `psvis-tracker:8000`
   pela rede compartilhada `waha_default`.

**O alerta nunca se perde** (camadas): playback falhou de vez → o serviço manda
o `fallback_text` como texto; o serviço está fora do ar → o próprio HA detecta
(`response_variable`) e manda o texto direto.

Endpoints: `POST /report` (`{"flight_id", "direction", "test", "force",
"chat_jid", "fallback_text", "sim", "announce"}` — todos opcionais;
`test:true` envia ao grupo SmokeTests; `force:true` ignora dedupe/atrasos;
`direction:"took_off"` agenda o update em voo e, com `announce:true`, manda
antes o cartão de decolagem — na mesma thread, em ordem; `sim:true` reconstrói
um voo já concluído: decolagem no instante do wheels-up, em voo truncado em
T+10, sem o destino do FR24 em retrospecto), `POST /backfill`, `GET /flights`,
`GET /health`, `GET /charts/<nome>.png`.

## Update em voo (T+10 da decolagem)

O FR24 quase nunca conhece o destino na decolagem. Estratégia intermediária
(decisão Eduardo 2026-08-29): no `took_off` o HA agenda no tracker um update
`ENROUTE_DELAY_S` (10 min) após a decolagem, com o trail ao vivo
(`clickhandler`, fallback playback): **rumo cardinal** (média circular dos
últimos headings), **mapa da rota até o momento** com a aeronave desenhada
como ícone de avião apontando o rumo, altitude/velocidade atuais e
**estimativas de chegada cruzadas com o flight log** — destinos anteriores
compatíveis com o rumo (±45°), **só por histórico** (sem estimativa por
distância): faixa em negrito do voo anterior mais rápido ao mais lento na
rota (mesma direção, senão reversa), ex. `*~14:44–14:50*`. Sem limite de
candidatos; ordenação pesa igualmente **alinhamento com o rumo** e
**frequência de voos na rota**. Depois das estimativas, um bloco `🌦️ Meteo
agora (METAR)` traz, por destino, a condição no destino (via
aviationweather.gov, sem chave; aeroporto sem METAR usa a estação mais
próxima, anotada — SSBL→SBNF) e, resumida, a condição *em rota* (estação mais
próxima do ponto médio restante). Meteo é best-effort: falha derruba só o
bloco, nunca o update. Dedupe por `enroute:<id>`; teste com voo já concluído: `sim:true`
trunca o trail nos primeiros 10 min.

**T+10 medido no trail, não na lista** (2026-10-02): o "real departure" de
`flight/list.json` pode ser o transponder ligado ainda no solo (41f01799:
10:20 na lista vs wheels-up 11:22), o que disparava o update a "há 0 min".
Antes de montar, o update lê a decolagem real do trail (1º ponto com
altitude > 0, `enroute.takeoff_ts`) e, se ainda não passaram
`ENROUTE_DELAY_S`, espera o restante (uma vez, sem gastar tentativa).

## Live watch (decolagem/pouso IMEDIATOS em qualquer aeroporto)

Os eventos do HA só são garantidos perto de Blumenau. Para paridade de
imediatismo em qualquer lugar, o tracker vigia o **feed ao vivo do FR24
filtrado pelo prefixo** (`feed.js?reg=PS-VIS` — o mesmo feed que a integração
do HA consome a cada 10 s para a área) a cada `FAST_POLL_S` (30 s), reagindo
às transições de `on_ground` como o HA faz: decolagem → texto na hora (origem
≠ `HOME_ICAO`/`HOME_IATA`; em BNU o HA anuncia) + update em voo agendado para
T+10 da decolagem real; pouso (flip para solo, ou sumiço em voo confirmado
pela chegada real na lista) → relatório completo imediato. Primeira visão em
pleno voo (restart no meio) não gera texto de decolagem falso (>15 min de
partida) mas ainda agenda/manda o update em voo. A hora de decolagem usada
aqui: transição solo→ar vista no feed → o próprio instante; primeira visão já
em voo com a lista dizendo >15 min → confere o trail ao vivo antes de
suprimir o texto (a lista pode trazer o horário do transponder no solo). Backups em camadas: watch
via lista (`AIRBORNE_POLL_S`, 5 min) e a varredura (15 min). Claims
(`takeoff:`/`enroute:<id>`) + um set de jobs em execução mantêm HA e watches
mutuamente exclusivos por voo.

## Flight log (base histórica para comparações)

Todo voo completado do PS-VIS — **nas duas direções** — é gravado em
`/data/flights.db` (SQLite): tabela `flights` (prefixo, rota com códigos e
coordenadas dos aeroportos, horários programados/reais, duração, cruzeiro
médio, máximos, distâncias) + `track_points` (o path exato: ts/lat/lon/
altitude/velocidade/vspeed/heading ponto a ponto) + playback bruto gzipado em
`/data/playbacks/<id>.json.gz`. Alimentação em duas vias: o próprio relatório
de pouso grava antes de enviar, e um **sync periódico** (`SYNC_INTERVAL_S`,
**15 min**) varre a lista FR24 e grava **e reporta** qualquer voo concluído
ainda não visto — é a captura universal: pouso do PS-VIS em **qualquer
aeroporto** chega ao grupo em ≤15 min mesmo sem evento do HA (eventos só
existem perto de Blumenau, ou onde for enquanto a aeronave estiver na lista
tracked em memória do FR24). Só voos novos no banco são reportados — restart e
backfill nunca geram spam. Todas as durações gravadas são wheels-up →
wheels-down (ver Fluxo §3) — são a base das ETAs do update em voo e da linha
`⏱️` do pouso. Recalcular o banco após mudar a regra: recomputar
`report.compute_stats` sobre cada `/data/playbacks/<id>.json.gz` e dar
`UPDATE` só em `dep_ts`/`arr_ts`/`duration_s` (preserva os horários
programados que só a lista tinha). Já comparado: duração vs média da rota.
Objetivo futuro: cruzeiro mais baixo? desvio de rota significativo?

APIs FR24 não-oficiais (as mesmas da integração HA) — sujeitas a mudança.

## Deploy

```bash
# arquivos ficam em /opt/psvis-tracker no LXC 101; depois:
python scripts/devtool.py guest bnu 101 "cd /opt/psvis-tracker && docker compose up -d --build"
```

Envie os arquivos com **fim de linha LF** (`git cat-file -p HEAD:<path>` ou
removendo `\r`): o checkout é CRLF e um `\r` depois do `\` de continuação do
Dockerfile quebra o `RUN` do `fonts-inter`. Vários passos (upload, `pct push`,
rebuild, logs) cabem numa única sessão paramiko — rajadas de sessões ao
bnu-proxmox derrubam o banner SSH (ver `REMOTE_ACCESS.md`).

`.env` no LXC (espelho do `.env` raiz do repo — ponto de drift, ver
`REMOTE_ACCESS.md` §5):

```
WAHA_API_KEY=      # BNU_WAHA_API_KEY
GROUP_JID=         # BNU_PSVIS_GROUP_JID (grupo "Aeronave PS-VIS")
TEST_GROUP_JID=    # SMOKETESTS_WHATSAPP_GROUP_JID
```

## Teste manual (grupo SmokeTests)

Sequência completa de um voo já concluído, em ordem — decolagem (sim) → em voo
T+10 (sim) → pouso 1/2 + 2/2:

```bash
python scripts/devtool.py guest bnu 101 "curl -s -X POST http://localhost:8790/report -H 'Content-Type: application/json' -d '{\"flight_id\":\"<id-fr24>\",\"direction\":\"took_off\",\"announce\":true,\"sim\":true,\"test\":true}'"
# quando o log mostrar 'en-route update for <id> sent', o pouso:
python scripts/devtool.py guest bnu 101 "curl -s -X POST http://localhost:8790/report -H 'Content-Type: application/json' -d '{\"flight_id\":\"<id-fr24>\",\"test\":true}'"
```

Caminho HA → tracker da decolagem (o mesmo payload da automação, com `test`/`sim`):

```bash
python scripts/devtool.py ha bnu POST "/api/services/rest_command/psvis_flight_report?return_response" '{"flight_id": "<id-fr24>", "direction": "took_off", "announce": true, "test": true, "sim": true, "fallback": "teste"}'
```

Reenviar ao grupo real = trocar `"test":true` por `"force":true` — só a pedido
do Eduardo (feito para 41f01799 em 2026-10-03).
