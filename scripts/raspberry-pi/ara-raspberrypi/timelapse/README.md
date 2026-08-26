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

```
CeuAzul/Timelapse/
├── por-do-sol-menos-20min/   YYYY-MM-DD_HHMM.jpg   lente PT, T−20
├── por-do-sol-menos-10min/   idem                  T−10
├── por-do-sol/               idem                  pôr do sol (T)
├── por-do-sol-mais-10min/    idem                  T+10
├── por-do-sol-mais-20min/    idem                  T+20
├── fixa/                     YYYY-MM-DD_HHMM_<m20|m10|pds|p10|p20>.jpg
│                             (gêmeo das 5 janelas na lente fixa)
└── Trabalho/YYYY-MM/         YYYY-MM-DD_HHMM.jpg   lente PT, 07:00–17:50
                              a cada 25 min — presença de trabalhadores
```

O pôr do sol (T) é calculado por dia (NOAA, embutido no script) para as
coordenadas do aeródromo Céu Azul — 26°33'41"S 48°41'46"W, UTC−3 fixo.
`Trabalho/` contém imagens de trabalhadores: manter a pasta **não
compartilhada** no Drive (uso interno de gestão da obra).

## Unidades (systemd, sem Docker — padrão do ara-raspberrypi)

| Unit | Agenda | Faz |
|---|---|---|
| `timelapse-trabalho.timer` | 07:00–17:50, exatos 25 min (27×/dia) | 1 frame PT → `outbox/Trabalho/` |
| `timelapse-sunset.timer` | 16:40 (cobre o T−20 mais cedo do ano, 17:09 jun) | dorme até cada janela; PT + fixa ×5 |
| `timelapse-upload.timer` | 20:00 diário, `Persistent=true` | `rclone move outbox → ceuazul:Timelapse` + poda `archive/` >30 d |

Fluxo local: cada frame vai a `/var/lib/timelapse/outbox/` e ganha hardlink
em `/var/lib/timelapse/archive/` (cópia de segurança local, 30 dias, custo
zero de espaço até o move). Starlink fora do ar → outbox acumula (SD de
58 GB ≈ anos) e o próximo upload drena.

| Arquivo | Cópia viva |
|---|---|
| [`timelapse-capture.py`](timelapse-capture.py) | `/usr/local/bin/timelapse-capture` (755) |
| `timelapse-{trabalho,sunset,upload}.{service,timer}` | `/etc/systemd/system/` |
| remote rclone `ceuazul` | `~eduardocenci/.config/rclone/rclone.conf` (600) — OAuth Google de eduardocenci@gmail.com, `root_folder_id` apontando para a pasta **CeuAzul**; backup do conf em `gitignore/ara-rclone.conf` no repo raiz |

## Operação

```bash
ssh eduardocenci@ara-raspberrypi "systemctl list-timers 'timelapse-*'"
ssh eduardocenci@ara-raspberrypi "journalctl -u timelapse-sunset -n 30 --no-pager"
ssh eduardocenci@ara-raspberrypi "ls -R /var/lib/timelapse/outbox | head"
ssh eduardocenci@ara-raspberrypi "systemctl start timelapse-upload"   # drenar agora
```

Premissa a vigiar nas primeiras semanas: a posição de descanso da lente PT
após o expediente. Se os frames de `por-do-sol*/` vierem com enquadramentos
diferentes entre dias, o auto-tracking não está voltando à posição de guarda
— o fallback é montar o timelapse com `fixa/`.
