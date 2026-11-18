# gpu-vigia — vigia da trava da GPU (bnu-proxmox)

Timer systemd **no host** `bnu-proxmox` que, a cada 60 s, procura a trava do
driver `amdgpu` e avisa o grupo **Casa SmokeTests** pelo WAHA. Não é container:
precisa enxergar os processos de todos os LXCs e sobreviver ao que trava dentro
deles.

## Por que existe

Em 17/11/2026 15:54 (EST do host; 17:54 em Brasília) o VCN da Radeon 680M deu
page fault e o reset de GPU do próprio kernel entrou em deadlock. Dali em
diante todo `ffmpeg` que abria `/dev/dri/renderD128` ficava em estado **D**
(imune a SIGKILL); o `canteiro-mosaico` (LXC 105, `Restart=always`) empilhou
~570 deles, carga 590, e o `/live` ficou ~16 h sem mosaico até alguém reparar.
O `canteiro-watchdog` não vê isso: o remédio dele (reiniciar go2rtc +
canteiro-hls) não destrava GPU.

## O que faz

| Situação | Critério | Ação |
|---|---|---|
| **Trava** | thread em estado D em 5 leituras seguidas do `/proc` (1 s entre elas), em 2 ticks seguidos — de um processo `ffmpeg`, ou de qualquer processo esperando em `amdgpu` / `drm_` / `dma_fence` | roda `pct exec 105 -- systemctl stop --no-block canteiro-mosaico` **uma vez** (para o empilhamento) e manda 🧊 "GPU travada desde HH:MM" |
| **Segue travada** | a cada 6 h enquanto durar | lembrete 🧊 |
| **Destravou** | primeiro tick sem nada preso | ✅ com a duração; se o host **não** reiniciou, religa o `canteiro-mosaico` (depois de um reboot ele sobe sozinho) |
| **Reset recuperado** | linhas `ring … timeout` / `recovered through reset` no log do kernel, sem nada preso (visto em 18/11/2026 12:27 EST) | ⚠️ nota, no máximo uma por hora, sem ação |

**Nunca reinicia o host nem mexe na GPU.** `amdgpu_gpu_recover` (debugfs) foi
testado em 18/11/2026 e só fica preso atrás do reset morto; o que destrava é
reiniciar o host, e isso é decisão do Eduardo. Mensagem que o WAHA recusar
fica no estado e sai no tick seguinte (o WAHA mora no LXC 101 do mesmo host).
Horários das mensagens em Brasília.

## Arquivos

| Arquivo | Cópia viva (bnu-proxmox) |
|---|---|
| [`gpu-vigia.py`](gpu-vigia.py) | `/usr/local/bin/gpu-vigia.py` |
| [`gpu-vigia.service`](gpu-vigia.service) / [`gpu-vigia.timer`](gpu-vigia.timer) | `/etc/systemd/system/` |
| estado | `/var/lib/gpu-vigia/state.json` |
| credenciais | `/etc/gpu-vigia.env` (600): `WAHA_URL`, `WAHA_KEY`, `WAHA_SESSION`, `NOTIFY_JID` — espelho de `BNU_WAHA_API_URL` / `BNU_WAHA_API_KEY` / `BNU_WAHA_SESSION` / `SMOKETESTS_WHATSAPP_GROUP_JID` do `.env` |

Tunáveis (env, todos opcionais): `SAMPLES` (5), `SAMPLE_GAP` (1.0),
`TICKS_TO_ALERT` (2), `REMIND_HOURS` (6), `NOTE_HOURS` (1), `COMMS`
(`ffmpeg`), `WCHAN_RE`, `STOP_CMD`, `START_CMD`, `HOST_LABEL`, `TZ_NAME`.

## Operar

```bash
python scripts/devtool.py run bnu-proxmox "systemctl list-timers gpu-vigia.timer --no-pager; cat /var/lib/gpu-vigia/state.json"
python scripts/devtool.py run bnu-proxmox "set -a; . /etc/gpu-vigia.env; set +a; python3 /usr/local/bin/gpu-vigia.py --dry-run"     # um tick sem efeito
python scripts/devtool.py run bnu-proxmox "set -a; . /etc/gpu-vigia.env; set +a; python3 /usr/local/bin/gpu-vigia.py --test-alert"  # uma linha de teste no SmokeTests
python scripts/devtool.py run bnu-proxmox "systemctl disable --now gpu-vigia.timer"                                                  # pausar
```

`state.json` com `"stuck": 0` e `last_tick` de menos de 2 min = vigia vivo e
GPU boa. O journal do host só recebe linha quando algo aconteceu (`journalctl
-u gpu-vigia`).

## Quando o 🧊 chegar

1. Confirmar: `ps -eo stat,wchan:32,comm | grep '^D'` e `journalctl -k | grep amdgpu | tail`.
2. O vigia já parou o `canteiro-mosaico`; o Frigate segue detectando sem a GPU.
3. Reiniciar o host (com o OK do Eduardo). Na volta o mosaico sobe sozinho e
   o vigia manda o ✅.

## Instalar / atualizar

O `ssh` puro desta máquina é recusado pelo host (`REMOTE_ACCESS.md`); os arquivos vão
por `devtool.ssh_run(dev, "cat > <destino>", input_bytes=…)` com fim de linha
LF, depois `systemctl daemon-reload && systemctl enable --now gpu-vigia.timer`.

## Testes

`python -m pytest scripts/proxmox/bnu-proxmox/gpu-vigia/test_gpu_vigia.py` —
`/proc` e log do kernel falsos (`PROC_ROOT`, `KLOG_FILE`), sem host. No host,
o mesmo `PROC_ROOT` falso com `STATE_DIR`/`STOP_CMD`/`HOST_LABEL` de teste
exercita o alerta de ponta a ponta (feito em 18/11/2026: 🧊 e ✅ no SmokeTests).
