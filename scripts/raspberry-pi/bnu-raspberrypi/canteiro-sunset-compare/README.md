# canteiro-sunset-compare — grades do pôr do sol: Dia/Semana/Mês de Trabalho (WhatsApp)

**Todo dia às 20:10 America/Sao_Paulo** o container `canteiro-sunset-compare`
no bnu-raspberrypi (supercronic — ver
[`../docker/canteiro-jobs/`](../docker/canteiro-jobs/); systemd timer até
2026-08-29) decide o que emitir. Cada produto é uma **grade 2×2** — o
MOSAICO das duas lentes, antes e depois (decisão Eduardo 14/11/2026: a
câmera não é mais movida; lente fixa à esquerda, olhando a rua, lente PT à
direita, parada na posição de repouso — juntas, "quase 180°"). As fotos são
as do pôr do sol do timelapse do ara Pi (upload às 20:00 — ver
[`../../ara-raspberrypi/timelapse/`](../../ara-raspberrypi/timelapse/)):

```
linha 1 = baseline   [ lentefixa | posicao1 ]
linha 2 = hoje       [ lentefixa | posicao1 ]      1200 px/célula → 2400×1350
```

Montada em PIL, sem alinhamento por software (sem excursões da câmera não
há tremor a corrigir). Quando a baseline é anterior a 14/11/2026, a legenda
ganha uma linha avisando que a linha de cima mostra o enquadramento antigo
das lentes.

| Produto | Quando | Baseline (linha 1) |
|---|---|---|
| 🌇 `Dia de Trabalho (DD/MM)` | seg–sex | ontem; segunda usa SEXTA |
| 🏗️ `Semana de Trabalho (seg - sex)` | sexta, após o Dia | sexta anterior |
| 📆 `Mês de Trabalho (26/MM - 25/MM)` | **dia 25, qualquer dia da semana** (janela de medição da empreiteira, 26→25) | dia 26 do mês anterior |

Envio via WAHA `sendImage` (funciona neste Core build; mesmo padrão do
[`../canteiro-watchdog/`](../canteiro-watchdog/)). A câmera queima
data/hora na faixa inferior de cada frame; cada célula recebe ainda uma
etiqueta `DD/MM` desenhada no canto superior direito.
Produtos do mesmo dia saem em sequência (Dia → Semana → Mês) e reusam os
downloads entre si.

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
- **Linha de cima sem as fotos das duas lentes → recua até 7 dias** (desde 29/10/2026):
  em 27/10 o barracão ficou sem energia no meio da sequência do pôr do sol
  (só T−20 saiu, sem `Luminancia125`), e em 28/10 a grade não saiu porque
  27/10 era a linha de base. Agora `make_grid_cells` recua dia a dia até
  `BASELINE_BACK_DAYS` por um dia completo, a legenda ganha
  `_linha de cima: 26/10 — 27/10 sem fotos no Drive_`, as etiquetas DD/MM
  seguem o dia usado, e o SmokeTests recebe um ℹ️. Sem `Luminancia125`
  num dia (seleção não rodou), cada célula tenta as janelas do pôr do sol
  na ordem `FALLBACK_WINS` (T, T−10, T+10, T−20, T+20) — antes só `por-do-sol`.
  Só "fotos de hoje faltando" continua a derrubar o produto (⚠️ no chat).
- `--test … --as-of AAAA-MM-DD` finge que "hoje" é outro dia (só com
  `--test`): serve para refazer uma grade perdida — a cópia no Drive
  (`DiaDeTrabalho/<dia>.jpg`) é gravada mesmo em teste, o envio vai ao
  `TEST_JID`. Usado em 29/10/2026 para refazer 27/10 e 28/10. Com
  `--no-archive` (desde 14/11/2026) a grade só é enviada, sem sobrescrever a
  arquivada no Drive — é o teste seguro de um dia que já tem grade.
- Última montagem enviada fica em `/tmp/ultima-comparacao.jpg` **dentro do
  container** (inspeção: `docker exec canteiro-sunset-compare ls -la /tmp`;
  some quando o container é recriado).
- Cada grade também é **arquivada no Drive** no topo do Timelapse:
  `DiaDeTrabalho/`, `SemanaDeTrabalho/` e `MesDeTrabalho/` +
  `YYYY-MM-DD.jpg` (séries prontas para consulta/IA, além do envio no
  WhatsApp; DiaDeTrabalho movida de `posicao1/` em 31/08/2026).
- **Destino de produção ATIVO desde 27/08/2026** (comando do Eduardo):
  grupo **Cenci Céu Azul Casa-Hangar** (`120363402090094156@g.us`). O JID
  do Casa SmokeTests fica comentado no env como rollback/staging.

## Fonte das fotos: `Luminancia125` (desde 11/09/2026)

As células vêm de `<pos>/Luminancia125/YYYY-MM-DD_HHMM.jpg` — a janela do pôr
do sol cujo frame de posicao1 tem luminância média mais perto de 125, escolhida
no ara ao fim da sequência (`timelapse-capture luminancia`; doutrina e
números em [`../../ara-raspberrypi/timelapse/`](../../ara-raspberrypi/timelapse/)).
Motivo: em `por-do-sol` fixo a luz variava de 100 a 128 conforme o clima;
com a seleção fica entre 123 e 126. Se um dia não tiver a pasta (seleção não
rodou), `make_grid_cells` cai para `por-do-sol` naquela célula e manda um
aviso ao SmokeTests (`ℹ️ … sem Luminancia125 para … — usei por-do-sol`). A
espera pelo upload das 20:00 (retries) vale para a pasta nova; o fallback é
tentado uma vez, no fim.

## Histórico (até 13/11/2026): grade 2×3 alinhada pelo pilar

Até 13/11/2026 a grade era 2×3 — a lente PT em três enquadramentos, colunas
posicao3 | posicao1 | posicao2, 800 px/célula (2400×900) — e, desde
07/09/2026, alinhada por software: `pilar_align.py` media o deslocamento do
pilar do galpão na posicao1 de cada dia (mesma correlação de fase da
re-âncora do ara, referências em `ceuazul:Timelapse/ref/`) e deslocava +
recortava as três colunas. Com a câmera parada isso saiu: `pilar_align.py`
foi apagado (e tirado da imagem `canteiro-jobs`), assim como as exceções de
baseline da estreia (31/08, 04/09 e 25/09/2026). As grades arquivadas no
Drive até 13/11 seguem no formato antigo.

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
docker exec canteiro-sunset-compare python3 /app/canteiro-sunset-compare.py --test                    # → TEST_JID, produtos de hoje
docker exec canteiro-sunset-compare python3 /app/canteiro-sunset-compare.py --test all                # força os 3 produtos
docker exec canteiro-sunset-compare python3 /app/canteiro-sunset-compare.py --test mes <chatId>       # um produto, chat específico
docker exec canteiro-sunset-compare python3 /app/canteiro-sunset-compare.py --test daily --as-of 2026-11-13 --no-archive   # refaz um dia sem tocar o Drive
```

A mensagem sai prefixada com `[TESTE]`. Smoke tests: 27/08/2026 (systemd,
montagem 26/08×27/08) e 29/08/2026 (container: aviso de foto faltando +
montagem 27/08×28/08 manual via ffmpeg) — ambos HTTP 201 no Casa
SmokeTests; 14/11/2026 (grade 2×2: `--test daily --as-of 2026-11-13
--no-archive`).
