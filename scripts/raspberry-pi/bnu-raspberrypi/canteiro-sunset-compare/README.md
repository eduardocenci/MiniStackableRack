# canteiro-sunset-compare — grades do pôr do sol: Dia/Semana/Mês de Trabalho (WhatsApp)

**Todo dia às 20:10 America/Sao_Paulo** o container `canteiro-sunset-compare`
no bnu-raspberrypi (supercronic — ver
[`../docker/canteiro-jobs/`](../docker/canteiro-jobs/); systemd timer até
2026-08-29) decide o que emitir. Cada produto é uma **grade 2×3** com as
fotos do pôr do sol nas TRÊS posições da lente PT (timelapse do ara Pi,
upload às 20:00 — ver
[`../../ara-raspberrypi/timelapse/`](../../ara-raspberrypi/timelapse/)):
linha 1 = baseline, linha 2 = hoje; colunas posicao3 | posicao1 | posicao2
(esquerda→centro→direita da obra); 800 px/célula. A grade é **alinhada por
software** (PIL) desde 07/09/2026 — ver abaixo; sem numpy/pillow ou sem as
referências do Drive, cai no modo antigo (ffmpeg hstack+vstack, 2400×900).

| Produto | Quando | Baseline (linha 1) | Exceção de estreia |
|---|---|---|---|
| 🌇 `Dia de Trabalho (DD/MM)` | seg–sex | ontem; segunda usa SEXTA | 31/08/2026 → domingo 30/08 |
| 🏗️ `Semana de Trabalho (seg - sex)` | sexta, após o Dia | sexta anterior | 04/09/2026 → domingo 30/08 |
| 📆 `Mês de Trabalho (26/MM - 25/MM)` | **dia 25, qualquer dia da semana** (janela de medição da empreiteira, 26→25) | dia 26 do mês anterior | 25/09/2026 → 31/08 (decisão Eduardo — sem imagens de 26/08) |

Envio via WAHA `sendImage` (funciona neste Core build; mesmo padrão do
[`../canteiro-watchdog/`](../canteiro-watchdog/)). A câmera queima
data/hora em cada frame; como o alinhamento recorta a faixa inferior onde
esse carimbo fica, cada célula recebe uma etiqueta `DD/MM` desenhada.
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

## Alinhamento por software (desde 07/09/2026)

Mesmo com a re-âncora da câmera, a guarda (pos1) varia até ~170 px de um dia
para o outro (a baseline do tracking caminha durante o dia; o passo mínimo do
motor, ~280 px de pan, não corrige resíduos menores — medido 03–06/09/2026).
No timelapse e nas grades isso vira um tremor visível entre dias. Como o
offset de cada frame é medível com ~4 px, o alinhamento é feito por software:

- [`pilar_align.py`](pilar_align.py) — MESMA medição da re-âncora do ara
  (correlação de fase do numpy na ROI do pilar do galpão, validade por
  concordância entre as 4 referências `posicao1-*.jpg`). As constantes (ROI,
  PSR, AGREE) são cópia da doutrina do ara
  ([`../../ara-raspberrypi/timelapse/`](../../ara-raspberrypi/timelapse/)) —
  se a ROI ou as referências mudarem lá, atualizar aqui.
- As referências vêm do backup no Drive `ceuazul:Timelapse/ref/` (o ara não é
  alcançável do bnu), baixadas uma vez por execução.
- Mede-se a **pos1** de cada dia; o offset daquele dia vale para as 3 colunas
  (pos2/pos3 são excursões reproduzíveis a partir da guarda, herdam o mesmo
  deslocamento). Recorta-se a janela de referência deslocada de `(sx, sy)`,
  travando o pilar no mesmo pixel em todas as células.
- **Fallbacks (a grade nunca deixa de sair):** sem numpy/pillow, sem as
  referências, ou medição inválida/implausível de uma linha → aquela linha
  não é deslocada; se nenhuma linha mede, cai na montagem ffmpeg de sempre.
- A imagem do container ganhou `numpy`+`pillow` (ver
  [`../docker/canteiro-jobs/`](../docker/canteiro-jobs/)). O log de cada
  envio diz `alinhada` ou `sem alinhamento`.

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
```

A mensagem sai prefixada com `[TESTE]`. Smoke tests: 27/08/2026 (systemd,
montagem 26/08×27/08) e 29/08/2026 (container: aviso de foto faltando +
montagem 27/08×28/08 manual via ffmpeg) — ambos HTTP 201 no Casa
SmokeTests.
