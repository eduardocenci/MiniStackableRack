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
script): **posição 2** (27/08/2026) `direita 0.4×0.5s ×2 → cima 0.4×0.2s`;
**posição 3** (30/08/2026) = espelho para o outro lado, `esquerda 0.4×0.5s
×2 → cima 0.4×0.2s`; volta = espelho com sinais invertidos. Fluxo por
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

Consumidor downstream: o container seg–sex das 20:10 no bnu-raspberrypi
([`../../bnu-raspberrypi/canteiro-sunset-compare/`](../../bnu-raspberrypi/canteiro-sunset-compare/))
compara o `posicao1/por-do-sol` do último dia útil vs hoje no WhatsApp e
arquiva cada montagem em `posicao1/DiaDeTrabalho/YYYY-MM-DD.jpg` —
mudanças na estrutura do Drive precisam acompanhar lá.

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

Premissas a vigiar nas primeiras semanas: (1) enquadramento estável entre
dias em `posicao1/` — se variar, o guard-return não está confiável e o
fallback é montar com `lentefixa/`; (2) acúmulo de resíduo nas principais de
T+10/T+20 do NASCER (3 ciclos de ida-e-volta sem correção de tracking antes
das 07:00) — se incomodar, plano B é a posicao2 só na última janela da
sequência.
