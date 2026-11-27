#!/usr/bin/env python3
"""canteiro-camera-saude — one report from the two records of the canteiro camera.

    python scripts/raspberry-pi/ara-raspberrypi/canteiro-relay-vigia/canteiro-camera-saude.py            # today
    python .../canteiro-camera-saude.py 2026-11-27                # one day
    python .../canteiro-camera-saude.py 2026-11-27 2026-12-03     # a range, one block per day + total
    python .../canteiro-camera-saude.py --json > saude.json       # the same numbers, for a notebook
    python .../canteiro-camera-saude.py --dir <folder>            # files already copied (offline)

Sources (run from a machine that reaches both, i.e. the dev PC):
  origem   ara-raspberrypi2:/var/lib/canteiro-relay-vigia/{eventos,amostras}-YYYY-MM.jsonl
           (canteiro-relay-vigia: what the CAMERA delivered to the relay - stalls of the video
           with the 2 min before each, byte rates a minute, ping and Wi-Fi of the camera)
  mosaico  bnu Frigate LXC 105:/var/lib/canteiro-mosaico/eventos-YYYY-MM.jsonl
           (canteiro-mosaico: what the PICTURE did - main holes per lens, substream on/off)

What it answers, per day: how often and for how long the camera dropped the video of each
stream, at what hours, what the streams and the link were doing just before, how long the
picture stood still, and whether every stall at the source was covered by the substream.
Stdlib only. Times are Brasília, as in the records.
"""
import argparse
import datetime
import json
import os
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# devtool.py needs the repo-root .env: MSR_ROOT points at a checkout that has it when this file
# is run from a clone without one.
ROOT = os.environ.get("MSR_ROOT") or os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
ARA = "eduardocenci@ara-raspberrypi2"
ARA_DIR = "/var/lib/canteiro-relay-vigia"
MOS_DIR = "/var/lib/canteiro-mosaico"
LENS = {"canteiro": "pt", "canteiro-alt": "fixa"}


def fetch(months, local):
    """-> (origem events, origem samples, mosaico events) as lists of dicts."""
    out = {"o_ev": [], "o_am": [], "m_ev": []}

    def lines(text, key):
        for ln in text.splitlines():
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    out[key].append(json.loads(ln))
                except ValueError:
                    pass

    for mo in months:
        if local:
            for key, name in (("o_ev", "eventos-%s.jsonl"), ("o_am", "amostras-%s.jsonl"),
                              ("m_ev", "mosaico-eventos-%s.jsonl")):
                p = os.path.join(local, name % mo)
                if os.path.isfile(p):
                    lines(open(p, encoding="utf-8").read(), key)
            continue
        for key, name in (("o_ev", "eventos"), ("o_am", "amostras")):
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", ARA,
                                "cat %s/%s-%s.jsonl 2>/dev/null" % (ARA_DIR, name, mo)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode == 255:
                print("AVISO: origem (ara-raspberrypi2) inalcançável: %s"
                      % r.stderr.decode("utf-8", "replace").strip()[:120], file=sys.stderr)
            lines(r.stdout.decode("utf-8", "replace"), key)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "devtool.py"), "guest", "bnu", "105",
                            "cat %s/eventos-%s.jsonl 2>/dev/null" % (MOS_DIR, mo)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
        if r.returncode or not os.path.isfile(os.path.join(ROOT, ".env")):
            print("AVISO: mosaico (LXC 105 via devtool em %s) não lido%s" % (
                ROOT, "" if os.path.isfile(os.path.join(ROOT, ".env")) else " - sem .env: aponte MSR_ROOT"),
                file=sys.stderr)
        lines(r.stdout.decode("utf-8", "replace"), "m_ev")
    return out["o_ev"], out["o_am"], out["m_ev"]


def ts(row):
    return datetime.datetime.strptime(row["ts"][:19].ljust(19, "0").replace("T", " ")[:16]
                                      + (row["ts"][16:19] if len(row["ts"]) >= 19 else ":00"), "%Y-%m-%d %H:%M:%S")


def q(vals, p):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))] if vals else None


def med(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def hiccup_before(ev):
    """In the 2 min trace of a `parou`: seconds before the stall at which EVERY other path dipped
    under half of its own median (a hiccup of the camera's link felt by all streams), or None."""
    a = ev.get("antes") or {}
    taxas, step = a.get("taxas") or {}, a.get("passo_s") or 5
    others = [v for k, v in taxas.items() if k != ev["path"]]
    if not others or not others[0]:
        return None
    n = len(others[0])
    meds = [med(v) or 0 for v in others]
    for i in range(n - 1, -1, -1):
        if all(m > 0 and (v[i] is None or v[i] < 0.5 * m) for v, m in zip(others, meds)):
            return (n - 1 - i) * step
    return None


def day_report(day, o_ev, o_am, m_ev):
    d = day.isoformat()
    oe = [e for e in o_ev if e["ts"].startswith(d)]
    oa = [e for e in o_am if e["ts"].startswith(d)]
    me = [e for e in m_ev if e["ts"].startswith(d)]
    rep = {"dia": d, "origem": {"streams": {}}, "mosaico": {"lentes": {}}}
    # ---- origem
    for path in sorted(set(e["path"] for e in oe if e.get("path")) | set(LENS)):
        stalls = [e for e in oe if e["path"] == path and e["ev"] == "parou"]
        backs = [e for e in oe if e["path"] == path and e["ev"] == "voltou"]
        durs = [e["s"] for e in backs]
        hic = [hiccup_before(e) for e in stalls if e.get("antes")]
        hours = {}
        for e in stalls:
            hours[e["ts"][11:13]] = hours.get(e["ts"][11:13], 0) + 1
        mins = [(a["paths"].get(path) or {}) for a in oa if a.get("paths")]
        rates = [m["kB_s"] for m in mins if m.get("kB_s") is not None]
        rep["origem"]["streams"][path] = {
            "paradas": len(stalls), "so_audio": sum(1 for e in stalls if e.get("so_audio")),
            "s_total": sum(durs), "s_mediana": med(durs), "s_p90": q(durs, 0.9), "s_max": max(durs) if durs else None,
            "taxa_antes_kB_s": med([e.get("antes_kB_s") for e in stalls]),
            "com_traco": len(hic), "soluco_antes": sum(1 for h in hic if h is not None),
            "soluco_s_antes_mediana": med([h for h in hic if h is not None]),
            "perda_ping_na_parada": sum(1 for e in stalls if ((e.get("ping") or {}).get("cam") or {}).get("perda")),
            "por_hora": dict(sorted(hours.items())),
            "kB_s_medio": round(statistics.mean(rates), 1) if rates else None,
            "minutos_sem_fonte": sum(1 for m in mins if m.get("pronto") is not None and m["pronto"] < 1),
            "fonte_caiu": sum(1 for e in oe if e["path"] == path and e["ev"] == "fonte_caiu"),
        }
    pings = [((a.get("ping") or {}).get("cam") or {}) for a in oa]
    wifi = [a["wifi"] for a in oa if a.get("wifi")]
    rep["origem"]["enlace"] = {
        "minutos_amostrados": len(oa),
        "minutos_com_perda": sum(1 for p in pings if p.get("perda")),
        "minutos_sem_resposta": sum(1 for p in pings if p.get("perda") == 100),
        "rtt_mediana_ms": med([p.get("rtt") for p in pings]), "rtt_pior_ms": max([p["rtt_max"] for p in pings if p.get("rtt_max")] or [None]),
        "sinal_dBm_mediana": med([w.get("sinal_dBm") for w in wifi]), "sinal_dBm_pior": min([w["sinal_dBm"] for w in wifi if w.get("sinal_dBm") is not None] or [None]),
        "snr_mediana": med([w.get("snr") for w in wifi]), "tx_Mbps_mediana": med([w.get("tx_Mbps") for w in wifi]),
        "tx_Mbps_pior": min([w["tx_Mbps"] for w in wifi if w.get("tx_Mbps") is not None] or [None]),
        "reassociacoes": sum(1 for e in oe if e["ev"] == "camera_reassociou"),
        "inicios_do_vigia": sum(1 for e in oe if e["ev"] == "inicio"),
    }
    # ---- mosaico
    for lens in ("fixa", "pt"):
        holes = [e for e in me if e.get("lente") == lens and e["ev"] == "principal_voltou"]
        subs = [e for e in me if e.get("lente") == lens and e["ev"] == "sub_entrou"]
        outs = [e for e in me if e.get("lente") == lens and e["ev"] == "sub_saiu"]
        hs = [e["s"] for e in holes]
        sub_at = [ts(e) for e in subs]
        held = 0.0
        for e in holes:                     # picture stood still: until the substream came, or the whole hole
            end = ts(e)
            start = end - datetime.timedelta(seconds=e["s"])
            w = [x for x, t in zip(subs, sub_at) if start <= t <= end + datetime.timedelta(seconds=2)]
            held += min(e["s"], w[0]["espera_s"]) if w else e["s"]
        rep["mosaico"]["lentes"][lens] = {
            "buracos_principal": len(holes), "ge_30s": sum(1 for x in hs if x >= 30), "s_total": round(sum(hs)),
            "s_max": max(hs) if hs else None,
            "sub_entrou": len(subs), "sub_espera_s_mediana": med([e["espera_s"] for e in subs]),
            "sub_espera_s_max": max([e["espera_s"] for e in subs] or [None]),
            "sub_na_tela_s": sum(e["s"] for e in outs), "sub_parou": sum(1 for e in me if e.get("lente") == lens and e["ev"] == "sub_parou"),
            "imagem_parada_s": round(held),
            "sozinha": sum(1 for e in me if e.get("lente") == lens and e["ev"] == "principal_parou" and e.get("outra_viva")),
        }
    rep["mosaico"]["encoder_reiniciado"] = sum(1 for e in me if e["ev"] == "encoder_reiniciado")
    rep["mosaico"]["feeder_reiniciado"] = sum(1 for e in me if e["ev"] == "feeder_reiniciado")
    rep["mosaico"]["inicios"] = sum(1 for e in me if e["ev"] == "inicio")
    # ---- join: every stall of a main at the source should have a substream on screen
    cov = {}
    for path, lens in LENS.items():
        stalls = [e for e in oe if e["path"] == path and e["ev"] == "parou"]
        sub_at = [ts(e) for e in me if e.get("lente") == lens and e["ev"] == "sub_entrou"]
        ok = sum(1 for e in stalls if any(abs((t - ts(e)).total_seconds()) <= 45 for t in sub_at))
        cov[lens] = {"paradas_na_origem": len(stalls), "cobertas_pelo_sub": ok}
    rep["cobertura"] = cov
    return rep


def show(rep):
    o, m = rep["origem"], rep["mosaico"]
    print("\n=== %s ===" % rep["dia"])
    print("ORIGEM (o que a câmera entregou ao relay)")
    for path, s in o["streams"].items():
        print("  %-17s paradas %3d (só áudio %d) · total %5s s · mediana %s · p90 %s · máx %s · taxa antes %s kB/s"
              % (path, s["paradas"], s["so_audio"], s["s_total"], s["s_mediana"], s["s_p90"], s["s_max"], s["taxa_antes_kB_s"]))
        if s["paradas"]:
            print("  %17s soluço de todos os streams antes: %d de %d com traço (mediana %s s antes) · ping com perda na hora: %d"
                  % ("", s["soluco_antes"], s["com_traco"], s["soluco_s_antes_mediana"], s["perda_ping_na_parada"]))
            print("  %17s por hora: %s" % ("", " ".join("%sh:%d" % kv for kv in s["por_hora"].items())))
        print("  %17s média do dia %s kB/s · minutos sem fonte %d · fonte caiu %d×"
              % ("", s["kB_s_medio"], s["minutos_sem_fonte"], s["fonte_caiu"]))
    e = o["enlace"]
    print("  enlace da câmera   %d min amostrados · com perda de ping %d · sem resposta %d · rtt mediana %s ms (pior %s)"
          % (e["minutos_amostrados"], e["minutos_com_perda"], e["minutos_sem_resposta"], e["rtt_mediana_ms"], e["rtt_pior_ms"]))
    print("  %17s  Wi-Fi sinal %s dBm (pior %s) · SNR %s · tx %s Mbps (pior %s) · reassociações %d · inícios do vigia %d"
          % ("", e["sinal_dBm_mediana"], e["sinal_dBm_pior"], e["snr_mediana"], e["tx_Mbps_mediana"], e["tx_Mbps_pior"],
             e["reassociacoes"], e["inicios_do_vigia"]))
    print("MOSAICO (o que a imagem fez)")
    for lens, s in m["lentes"].items():
        print("  %-5s buracos do principal %3d (≥30 s: %d; só esta lente: %d) · total %5s s · máx %s · sub entrou %d× "
              "(espera mediana %s s, máx %s) · sub na tela %s s · IMAGEM PARADA %s s"
              % (lens, s["buracos_principal"], s["ge_30s"], s["sozinha"], s["s_total"], s["s_max"], s["sub_entrou"],
                 s["sub_espera_s_mediana"], s["sub_espera_s_max"], s["sub_na_tela_s"], s["imagem_parada_s"]))
    print("  encoder reiniciado %d · feeder reiniciado %d · inícios do compositor %d"
          % (m["encoder_reiniciado"], m["feeder_reiniciado"], m["inicios"]))
    print("COBERTURA  " + " · ".join("%s: %d de %d paradas na origem com sub na tela"
                                     % (k, v["cobertas_pelo_sub"], v["paradas_na_origem"]) for k, v in rep["cobertura"].items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("de", nargs="?", help="AAAA-MM-DD (default: hoje)")
    ap.add_argument("ate", nargs="?", help="AAAA-MM-DD (default: = de)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dir", help="pasta com eventos-/amostras-/mosaico-eventos-AAAA-MM.jsonl já copiados")
    a = ap.parse_args()
    brt = datetime.timezone(datetime.timedelta(hours=-3))
    d0 = datetime.date.fromisoformat(a.de) if a.de else datetime.datetime.now(brt).date()
    d1 = datetime.date.fromisoformat(a.ate) if a.ate else d0
    days = [d0 + datetime.timedelta(days=i) for i in range((d1 - d0).days + 1)]
    months = sorted(set(d.strftime("%Y-%m") for d in days))
    o_ev, o_am, m_ev = fetch(months, a.dir)
    reps = [day_report(d, o_ev, o_am, m_ev) for d in days]
    if a.json:
        json.dump(reps, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for r in reps:
        show(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
