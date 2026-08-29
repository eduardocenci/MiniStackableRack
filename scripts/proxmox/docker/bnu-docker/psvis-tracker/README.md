# psvis-tracker — relatório pós-voo do PS-VIS

Container no LXC 101 (`10.1.1.126`, porta **8790**) que envia **a mensagem
única de pouso do PS-VIS**: gráfico de altitude + velocidade e mapa do
trajeto, com legenda completa (rota, horários, modelo, cruzeiro, link FR24),
ao grupo WhatsApp **"Aeronave PS-VIS"** via WAHA (decisão Eduardo 2026-08-29 — o grupo da família não recebe mais alertas de voo).

## Fluxo (uma mensagem por pouso — decisão Eduardo 2026-08-29)

1. No pouso, a automação `psvis_blumenau_flight_alert` no bnu-homeassistant
   (`scripts/proxmox/homeassistant/bnu-homeassistant/packages/flightradar_psvis.yaml`)
   **não manda texto** — chama `POST http://10.1.1.126:8790/report` com o
   `flight_id` do evento FR24 e um `fallback_text` (o texto enriquecido
   renderizado no HA). Decolagens continuam como texto direto do HA.
2. O serviço espera `INITIAL_DELAY_S` (o FR24 leva ~1–2 min para finalizar o
   track após o toque) e busca o **playback** do voo — o trail completo com
   altitude/velocidade ponto a ponto. Se o `flight_id` não vier, resolve o voo
   mais recente do PS-VIS com pouso real via `flight/list.json`.
3. Calcula: altitude de cruzeiro e velocidade de cruzeiro (**média no trecho
   contíguo estabilizado** a ≥95 % da altitude máxima), velocidade máxima,
   duração e distância da rota, e monta a legenda inteira a partir do playback.
4. Renderiza o gráfico (matplotlib, dois painéis empilhados — nunca eixo
   duplo) + mapa OSM e envia via WAHA `sendImage`. O PNG é servido em
   `/charts/<id>.png` porque o WAHA Core busca imagens por URL — resolve
   `psvis-tracker:8000` pela rede compartilhada `waha_default`.

**O alerta nunca se perde** (camadas): playback falhou de vez → o serviço manda
o `fallback_text` como texto; o serviço está fora do ar → o próprio HA detecta
(`response_variable`) e manda o texto direto.

Endpoints: `POST /report` (`{"flight_id", "direction", "test", "force",
"chat_jid", "fallback_text", "sim"}` — todos opcionais; `test:true` envia ao
grupo SmokeTests; `direction:"took_off"` agenda o update em voo),
`POST /backfill`, `GET /flights`, `GET /health`, `GET /charts/<id>.png`.

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

## Airborne watch (decolagem em qualquer aeroporto)

Os eventos do HA só são garantidos perto de Blumenau, então o tracker também
vigia sozinho: a cada `AIRBORNE_POLL_S` (5 min) consulta a lista FR24; ao ver
o PS-VIS no ar (partida real sem chegada, com menos de 45 min) manda o texto
de decolagem — só para origem ≠ `HOME_ICAO` (SSBL), onde o HA não alcança —
e agenda o update em voo para T+10 da decolagem real. Marcadores de claim
(`takeoff:<id>`, `enroute:<id>`) tornam o caminho HA e o watch mutuamente
exclusivos por voo.

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
backfill nunca geram spam. Objetivo futuro: comparar
um voo novo com o histórico (mais lento? cruzeiro mais baixo? desvio de rota
significativo?).

APIs FR24 não-oficiais (as mesmas da integração HA) — sujeitas a mudança.

## Deploy

```bash
# arquivos ficam em /opt/psvis-tracker no LXC 101; depois:
python scripts/devtool.py guest bnu 101 "cd /opt/psvis-tracker && docker compose up -d --build"
```

`.env` no LXC (espelho do `.env` raiz do repo — ponto de drift, ver
`REMOTE_ACCESS.md` §5):

```
WAHA_API_KEY=      # BNU_WAHA_API_KEY
GROUP_JID=         # BNU_PSVIS_GROUP_JID (grupo "Aeronave PS-VIS")
TEST_GROUP_JID=    # SMOKETESTS_WHATSAPP_GROUP_JID
```

## Teste manual (grupo SmokeTests)

```bash
python scripts/devtool.py guest bnu 101 "curl -s -X POST http://localhost:8790/report -H 'Content-Type: application/json' -d '{\"flight_id\":\"<id-fr24>\",\"test\":true}'"
```
