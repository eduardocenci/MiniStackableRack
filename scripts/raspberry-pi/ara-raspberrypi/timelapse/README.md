# timelapse — frames diários do canteiro ARA → Google Drive `CeuAzul/Timelapse/`

Fotografa a obra todos os dias a partir do relay mediamtx local (sem
credenciais da câmera) e sobe tudo às 20:00 para o Drive. Duplo propósito,
decidido por Eduardo em 26/08/2026:

1. **Timelapse** — 1 foto/dia por janela solar, lente PT (após o expediente a
   inatividade deixa o auto-tracking parado ⇒ enquadramento constante; sem
   preset ONVIF neste firmware, ver [`../ptz/README.md`](../ptz/README.md)).
   A lente fixa é capturada junto como garantia de enquadramento (inverno: o
   pôr do sol acontece antes das 18:00 e o time ainda está na obra).
2. **Ground-truth para IA** — progresso da obra ("o que mudou entre X e Y?")
   e registro de presença de trabalhadores. A câmera grava data/hora no
   próprio frame.

## Layout no Drive (e no outbox local, 1:1)

Estrutura **posição/janela/data.jpg** (decisão Eduardo 27/08/2026; até então
as janelas ficavam na raiz e a lente fixa usava sufixos `_tag` — migrado no
mesmo dia):

```
CeuAzul/Timelapse/
├── posicao1/<janela>/        YYYY-MM-DD_HHMM.jpg — lente PT na GUARDA
│                             (foto principal de cada janela, T+0:00)
├── posicao2/<janela>/        lente PT na POSIÇÃO 2 (direita, ~T+0:40)
├── posicao3/<janela>/        lente PT na POSIÇÃO 3 (esquerda, espelho da
│                             posição 2, ~T+2:00)
├── lentefixa/<janela>/       lente fixa, disparada junto da principal
└── trabalho/YYYY-MM/         lente PT na guarda, 07:00–18:00 a cada
                              15 min — presença (25 min até 26/08/2026)

<janela> = nascer-do-sol | nascer-do-sol-mais-10min | nascer-do-sol-mais-20min
         | por-do-sol-menos-20min | por-do-sol-menos-10min | por-do-sol
         | por-do-sol-mais-10min | por-do-sol-mais-20min
```

Nascer e pôr do sol são calculados por dia (NOAA, embutido no script) para
as coordenadas do aeródromo Céu Azul — 26°33'41"S 48°41'46"W, UTC−3 fixo.
`trabalho/` contém imagens de trabalhadores: manter a pasta **não
compartilhada** no Drive (uso interno de gestão da obra).

## Posições 2 e 3 (dead-reckoning calibrado)

Este firmware não tem preset ONVIF ([`../ptz/README.md`](../ptz/README.md)),
mas toda excursão parte da guarda — replayar a MESMA sequência de bursts
reproduz o enquadramento. Receitas calibradas por Eduardo (`RECIPES` no
script): **posição 2** (27/08/2026) `direita 0.4×1.1s → cima 0.4×0.2s`;
**posição 3** (30/08/2026) = espelho para o outro lado, `esquerda 0.4×1.1s
→ cima 0.4×0.2s`; volta = espelho com sinais invertidos. (Eram 2 bursts
de 0.5 s de pan; fundidos em 01/09/2026 — menos eventos de `Stop`, menos
folga/latência. 1.1 s e não 1.0 porque cada burst carrega um "quantum de
latência": medido contra o enquadramento 2×0.5 s, 1.0 s ficava ~1 quantum
aquém e 1.1 s casou — pos2 (−24,−24) px, pos3 (−84,−56) px.) Fluxo por
janela solar: principal (posicao1 + lentefixa) → 30 s → receita 2 → foto →
reverso → receita 3 → foto → reverso. Resíduo ±3–4% por ciclo; o
guard-return do firmware zera no primeiro trabalhador rastreado do dia (o
retorno automático NÃO dispara após movimento manual — sondado 27/08/2026,
95 s parado). Health-check: `timelapse-capture pos2test` / `pos3test`
(foto em /tmp do container, sem tocar o outbox; mexem a câmera). Os
movimentos saem do próprio Pi pela LAN — internet não participa da captura.

## Agendas (container `canteiro-timelapse` desde 2026-08-29)

Roda no container `canteiro-timelapse`
([`../docker/canteiro-timelapse/`](../docker/canteiro-timelapse/), supercronic
com `TZ=America/Sao_Paulo`; até 2026-08-29 eram 4 systemd timers — os units
seguem no Pi desabilitados como rollback por uma onda):

| Entrada do crontab | Agenda | Faz |
|---|---|---|
| `timelapse-capture sunrise` | 05:00 (cobre o nascer mais cedo do ano, ~05:15 dez) | dorme até cada janela; posicao1 + lentefixa + posicao2 + posicao3 ×3 |
| `timelapse-capture trabalho` | 07:00–18:00, a cada 15 min (45×/dia) | 1 frame PT → `outbox/trabalho/` |
| `timelapse-capture sunset` | 16:40 (cobre o T−20 mais cedo do ano, 17:09 jun) | dorme até cada janela; posicao1 + lentefixa + posicao2 + posicao3 ×5 |
| `rclone move …` | 20:00 diário **+ na partida do container** | `rclone move outbox → ceuazul:Timelapse` |

O upload na partida do container (entrypoint) substitui o `Persistent=true`
do timer antigo: queda de energia no barracão → o Pi volta → o Docker sobe o
container → o outbox drena na hora, sem esperar as 20:00. `rclone move` é
idempotente, então a repetição é inócua.

Fluxo local: cada frame vai a `/var/lib/timelapse/outbox/` (bind mount — o
mesmo dir da era systemd) e o `rclone move` **apaga do Pi assim que a
transferência é confirmada** — nenhuma cópia local é mantida (decisão
Eduardo 26/08/2026; o Drive é o único arquivo). Starlink fora do ar →
outbox acumula (SD de 58 GB ≈ anos) e o próximo upload drena.

| Arquivo | Cópia viva |
|---|---|
| [`timelapse-capture.py`](timelapse-capture.py) | `~/canteiro-timelapse/timelapse-capture` (COPY no build, vira `/usr/local/bin/timelapse-capture` na imagem) |
| [`../ptz/canteiro-ptz.py`](../ptz/canteiro-ptz.py) | idem (`/usr/local/bin/canteiro-ptz` na imagem); credenciais em `~/canteiro-timelapse/env/canteiro-ptz.env`, montado ro em `/etc/canteiro-ptz.env` |
| remote rclone `ceuazul` | `~eduardocenci/.config/rclone/rclone.conf` (600, montado rw no container — uid 1000 preserva o dono no refresh do token) — OAuth Google de eduardocenci@gmail.com, `root_folder_id` apontando para a pasta **CeuAzul**; backup do conf em `gitignore/ara-rclone.conf` no repo raiz |

Consumidor downstream: o container diário das 20:10 no bnu-raspberrypi
([`../../bnu-raspberrypi/canteiro-sunset-compare/`](../../bnu-raspberrypi/canteiro-sunset-compare/))
monta as grades 2×3 do pôr do sol (Dia seg–sex, Semana às sextas, Mês no
dia 25; colunas pos3|pos1|pos2) no WhatsApp e arquiva em
`DiaDeTrabalho/`, `SemanaDeTrabalho/` e `MesDeTrabalho/` no topo do
Timelapse — mudanças na estrutura do Drive precisam acompanhar lá.

## Operação

```bash
ssh eduardocenci@ara-raspberrypi "docker logs canteiro-timelapse --tail 30"
ssh eduardocenci@ara-raspberrypi "ls -R /var/lib/timelapse/outbox | head"
ssh eduardocenci@ara-raspberrypi "docker restart canteiro-timelapse"   # drenar agora (upload na partida)
```

O subcomando `trabalho` recusa rodar fora de 07:00–18:00 (guarda no
script); para exercitar a captura fora do expediente use `docker exec
canteiro-timelapse timelapse-capture pos2test` (mexe a câmera!) ou um grab
ffmpeg direto do relay.

Lição operacional (30/08/2026): sessão manual de calibração/testes SEM
tracking ativo (domingo) acumulou ~5% de tilt após 3 vai-e-voltas — sempre
encerrar comparando um snap com a referência da guarda e corrigindo com
nudge antes de sair (o guard-return só zera com gente rastreada na obra).

## Re-âncora visual da guarda (desde 31/08/2026)

Descoberta de 31/08 (dia de instalação das telas colada na câmera): **a
"guarda" do firmware não é um preset absoluto — é "volte para onde estava
quando o tracking começou", e essa baseline ANDA quando trackings se
encadeiam** (o pré-track do evento B é o meio do track do A). Em dias de
trabalho intenso perto da câmera a posição 1 pode migrar (aconteceu:
~6–10% à esquerda entre 12:00 e 15:00; lente fixa intacta provou que não
foi esbarrão físico).

Medição de 01/09/2026 (sequência limpa, sem gente): a própria coreografia
deriva — nascer 06:27 (+4,+16) px → +10 min (+156,**−136**) → +20 min
(+468,**−168**). **Tilt sempre para baixo** (gravidade a favor na descida,
contra na subida — o mesmo 0.2 s não rende o mesmo ângulo) e **pan por
backlash** na inversão de sentido: ~150–300 px por janela (2 excursões).
Re-ancorar só antes da 1ª janela não bastava.

Correção: `timelapse-capture reanchor [--dry]` — correlação de fase
(numpy/Pillow, na imagem do container) entre um snap atual e a
**biblioteca de referências** `/var/lib/timelapse/ref/posicao1-*.jpg`
(`-dawn` 31/08 06:28, `-day` 30/08 14:46, `-dusk` 01/09 17:42; backup em
`ceuazul:Timelapse/ref/`) — usa a de **maior pico**, i.e. a iluminação
mais parecida com a de agora (uma só referência de madrugada caía a
conf≈0.02 no crepúsculo e bloqueava a correção quando mais precisava).
Offset medido na **ROI do pilar em Y do galpão** (constante `ROI`; o
pilar é estrutura da câmera — a obra evolui, ele não — decisão Eduardo
31/08/2026). **Lei de controle** (mapeada 01/09/2026 à noite, ~40 bursts
medidos): o motor de pan tem um **quantum mínimo de ~280 px por comando**
— v≤0.12 não move; v=0.15–0.25 → ~280–380 px; v=0.4 → ~400 px; duração
<0.25 s não modula (latência HTTP `ContinuousMove`→`Stop` + rampa dominam)
e o `<Timeout>` do ONVIF, embora honrado, é errático abaixo de 0.2 s e
dá o mesmo quantum a 0.2 s. Tilt: quantum ~180 px (0.2 s a 0.4). Logo a
precisão fisicamente possível é **±140 px** e a lei é: corrigir só acima
de 200 px (pan; o quantum varia 280–400, abaixo de 200 é cara-ou-coroa)
/ 100 px (tilt) — abaixo disso o quantum pioraria —, com
v 0.4 para erros ≥350 px e v 0.2 para os demais; bursts de 0.2 s.
Corrige e re-mede (até 4 iterações, tolerância 80 px, gate de confiança
0.02, janela de busca ±500 px), avaliando **por eixo**: eixo que piorou
tem a correção desfeita, o outro fica; eixo que não se moveu (<20 px,
stall) é aceito sem insistir; os dois piorando = aborta — nunca vaga. Roda ~60 s **antes de CADA janela** e **após cada
volta** de pos2 e de pos3 (a última volta da sequência deixa a câmera na
guarda para `trabalho/` e para a noite); falha de importação/confiança
nunca bloqueia as fotos (prossegue sem corrigir e loga). Logs em tempo
real (`PYTHONUNBUFFERED=1` no compose — antes só apareciam no fim do
processo).

Calibração noturna de 01/09/2026 (10 ciclos completos 1→2→1→3→1 sem
re-âncora, medidos ciclo a ciclo): **6 voltaram com erro (0,0)** — a
coreografia é reproduzível ao pixel — e 4 tiveram um **salto discreto**
de ~150–360 px de pan (sinal constante dentro de cada run, nunca
fracionário), assinatura de **backlash intermitente na inversão de
sentido**. Não é viés acumulativo: as velocidades das receitas NÃO devem
ser "compensadas" (errariam nos ciclos que já dão zero) — a resposta é a
malha fechada após cada volta. Hipótese para o futuro, se sobrar
apetite: "pousar" sempre pelo mesmo lado (overshoot + burst final na
direção canônica), o truque clássico de CNC contra folga.

Referências precisam ser **mutuamente consistentes**: a noturna gravada
com a câmera a (+60,−68) da madrugada viciou as medições até ser
regravada; a diurna de domingo (−80,+68) foi removida — gravar uma nova
só com a câmera confirmada na guarda (`reanchor --dry` ≈ 0 contra a
madrugada).

Manutenção das referências: quando a guarda for deliberadamente
REPOSICIONADA, gravar novos dourados (frames bons de posicao1 nas três
iluminações) em `/var/lib/timelapse/ref/posicao1-<tag>.jpg` +
`rclone sync` para `ceuazul:Timelapse/ref/` — e conferir se a `ROI` do
pilar ainda vale.

Premissas a vigiar nas primeiras semanas: (1) enquadramento estável entre
dias em `posicao1/` — se variar, o guard-return não está confiável e o
fallback é montar com `lentefixa/`; (2) acúmulo de resíduo nas principais de
T+10/T+20 do NASCER (3 ciclos de ida-e-volta sem correção de tracking antes
das 07:00) — se incomodar, plano B é a posicao2 só na última janela da
sequência.
