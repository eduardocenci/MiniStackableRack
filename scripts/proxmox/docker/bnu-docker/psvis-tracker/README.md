# psvis-tracker — relatório pós-voo do PS-VIS

Container no LXC 101 (`10.1.1.126`, porta **8790**) que envia **a mensagem
única de pouso do PS-VIS**: gráfico de altitude + velocidade e mapa do
trajeto, com legenda completa (rota, horários, modelo, cruzeiro, link FR24),
ao grupo WhatsApp Casa Blumenau via WAHA.

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
"chat_jid", "fallback_text"}` — todos opcionais; `test:true` envia ao grupo
SmokeTests), `GET /health`, `GET /charts/<id>.png`.

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
GROUP_JID=         # BNU_WHATSAPP_GROUP_JID
TEST_GROUP_JID=    # SMOKETESTS_WHATSAPP_GROUP_JID
```

## Teste manual (grupo SmokeTests)

```bash
python scripts/devtool.py guest bnu 101 "curl -s -X POST http://localhost:8790/report -H 'Content-Type: application/json' -d '{\"flight_id\":\"<id-fr24>\",\"test\":true}'"
```
