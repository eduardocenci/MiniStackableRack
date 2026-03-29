#!/bin/bash
# Boiler Report Setup Script
# Paste this entire script into the HA Terminal (sidebar → Terminal & SSH add-on)
# Run with: bash setup_boiler_report.sh
# Or paste and run directly in the terminal.

set -e

echo "=== Boiler Report Setup ==="

# 1. Create directories
mkdir -p /config/packages
mkdir -p /config/scripts
mkdir -p /config/www/boiler_reports

# 2. Check if packages is enabled in configuration.yaml
if ! grep -q "packages" /config/configuration.yaml; then
  echo ""
  echo "⚠️  ACTION NEEDED: Add this to /config/configuration.yaml under 'homeassistant:':"
  echo ""
  echo "homeassistant:"
  echo "  packages: !include_dir_named packages"
  echo ""
  echo "Then re-run this script after saving."
  echo "(Open configuration.yaml in the Studio Code add-on to add it)"
fi

# 3. Write the HA package YAML
cat > /config/packages/boiler_report.yaml << 'YAML_EOF'
# Boiler Monthly Report — HA Package

recorder:
  purge_keep_days: 400
  include:
    entities:
      - sensor.boiler_current_temperature
      - switch.boiler_termostato

template:
  - sensor:
      - name: "Boiler Current Temperature"
        unique_id: "boiler_current_temperature_report"
        unit_of_measurement: "°C"
        device_class: temperature
        state_class: measurement
        state: >
          {{ state_attr('water_heater.boiler_boiler', 'current_temperature') | float(0) }}
        availability: >
          {{ state_attr('water_heater.boiler_boiler', 'current_temperature') is not none
             and states('water_heater.boiler_boiler') not in ['unavailable', 'unknown'] }}

  - button:
      - name: "Send Boiler Report Now"
        unique_id: "send_boiler_report_button"
        icon: mdi:email-send
        press:
          - action: script.boiler_monthly_report

input_text:
  boiler_report_recipients:
    name: "Boiler Report Recipients"
    icon: mdi:email-multiple
    initial: "eduardocenci@gmail.com"
    max: 255

shell_command:
  run_boiler_report: "python3 /config/scripts/boiler_report.py >> /config/boiler_report.log 2>&1"

script:
  boiler_monthly_report:
    alias: "Send Boiler Monthly Report"
    icon: mdi:chart-line
    mode: single
    sequence:
      - action: shell_command.run_boiler_report
        alias: "Generate and send report"

automation:
  - id: "boiler_end_of_month_report"
    alias: "Boiler — End of Month Report"
    description: "Auto-send boiler report on the last day of each month"
    trigger:
      - platform: time
        at: "23:55:00"
    condition:
      - condition: template
        value_template: >
          {{ (now() + timedelta(days=1)).day == 1 }}
    action:
      - action: script.boiler_monthly_report
    mode: single
YAML_EOF
echo "✓ Written /config/packages/boiler_report.yaml"

# 4. Write the Python script
cat > /config/scripts/boiler_report.py << 'PYEOF'
#!/usr/bin/env python3
"""
Boiler Monthly Report Generator — /config/scripts/boiler_report.py
Reads credentials from /config/secrets.yaml, queries HA history,
generates 3 matplotlib charts, sends HTML email with inline images.
"""
import os, io, calendar, datetime, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import requests, yaml
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

HA_URL        = "http://localhost:8123"
ENTITY_TEMP   = "sensor.boiler_current_temperature"
ENTITY_SWITCH = "switch.boiler_termostato"
REPORT_DIR    = "/config/www/boiler_reports"
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 465
C_BLUE, C_ORANGE, C_HIGHLIGHT, C_BG = "#1565C0", "#E65100", "#F9A825", "#FAFAFA"

def load_secrets():
    with open("/config/secrets.yaml") as f:
        return yaml.safe_load(f)

def _hdrs(token):
    return {"Authorization": f"Bearer {token}"}

def get_history(token, entity_id, start, end):
    url = (f"{HA_URL}/api/history/period/{start.strftime('%Y-%m-%dT%H:%M:%S')}"
           f"?end_time={end.strftime('%Y-%m-%dT%H:%M:%S')}"
           f"&filter_entity_id={entity_id}&minimal_response=false&no_attributes=true")
    r = requests.get(url, headers=_hdrs(token), timeout=120)
    r.raise_for_status()
    data = r.json()
    return data[0] if data else []

def month_range(y, m):
    start = datetime.datetime(y, m, 1)
    end = datetime.datetime(y, m, calendar.monthrange(y, m)[1], 23, 59, 59)
    return start, end

def compute_max_temp(states):
    vals = []
    for s in states:
        try:
            v = float(s["state"])
            if 0 < v < 120: vals.append(v)
        except: pass
    return max(vals) if vals else None

def compute_on_hours(states):
    total, prev_t, prev_s = 0.0, None, None
    for s in states:
        raw = s.get("last_changed") or s.get("last_updated", "")
        try: t = datetime.datetime.fromisoformat(raw.replace("Z","+00:00")).replace(tzinfo=None)
        except: continue
        if prev_s == "on" and prev_t: total += (t - prev_t).total_seconds() / 3600
        prev_t, prev_s = t, s.get("state")
    return total

def get_timed_temps(states):
    res = []
    for s in states:
        raw = s.get("last_changed") or s.get("last_updated", "")
        try:
            t = datetime.datetime.fromisoformat(raw.replace("Z","+00:00")).replace(tzinfo=None)
            v = float(s["state"])
            if 0 < v < 120: res.append((t, v))
        except: pass
    return res

def _save_fig(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=C_BG, bbox_inches="tight")
    plt.close(fig); buf.seek(0); return buf.read()

def chart_daily(pts, label):
    fig, ax = plt.subplots(figsize=(9, 3.5), facecolor=C_BG); ax.set_facecolor(C_BG)
    if pts:
        times, temps = zip(*pts)
        ax.plot(times, temps, color=C_BLUE, linewidth=1.6, alpha=0.9)
        ax.fill_between(times, temps, alpha=0.08, color=C_BLUE)
        mx = max(temps); mt = times[list(temps).index(mx)]
        ax.scatter([mt],[mx],color="red",zorder=5,s=55)
        ax.annotate(f"  {mx:.1f} °C", xy=(mt,mx), fontsize=8.5, color="red", va="bottom")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%-d %b"))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
    else:
        ax.text(0.5,0.5,"No data",ha="center",va="center",transform=ax.transAxes,color="#aaa",fontsize=11)
    ax.set_title(f"Water Temperature — {label}", fontsize=11, pad=8)
    ax.set_ylabel("°C", fontsize=9); ax.tick_params(labelsize=8)
    ax.spines[["top","right"]].set_visible(False); fig.tight_layout()
    return _save_fig(fig)

def chart_bar(labels, values, title, ylabel, color):
    fig, ax = plt.subplots(figsize=(9, 3.5), facecolor=C_BG); ax.set_facecolor(C_BG)
    safe = [v if v else 0 for v in values]
    colors = [C_HIGHLIGHT if i==len(safe)-1 else color for i in range(len(safe))]
    bars = ax.bar(labels, safe, color=colors, width=0.6, zorder=2)
    mx = max(safe) if safe else 1
    for bar, v in zip(bars, safe):
        if v > 0:
            ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+mx*0.015,
                    f"{v:.1f}", ha="center", va="bottom", fontsize=7.5)
    ax.set_title(title, fontsize=11, pad=8); ax.set_ylabel(ylabel, fontsize=9)
    ax.tick_params(axis="x", labelsize=7.5, rotation=45); ax.tick_params(axis="y", labelsize=8)
    ax.spines[["top","right"]].set_visible(False); ax.grid(axis="y", alpha=0.25, zorder=1)
    fig.tight_layout(); return _save_fig(fig)

def badge(pct):
    if pct is None: return '<span style="color:#9E9E9E">N/A</span>'
    c = "#2E7D32" if pct>=0 else "#C62828"; a = "▲" if pct>=0 else "▼"
    return f'<span style="color:{c};font-weight:600">{a} {abs(pct):.1f}%</span>'

def pct(a, b):
    return None if (a is None or b is None or b==0) else (a-b)/b*100

def build_html(cur_label, monthly, dt, do_):
    cur = monthly[-1]
    on_h = int(cur["on_hours"]); on_m = int(round((cur["on_hours"]%1)*60))
    mx = f"{cur['max_temp']:.1f} °C" if cur["max_temp"] else "—"
    prev_label = monthly[0]["label"]
    rows = ""
    for i, m in enumerate(monthly):
        h=int(m["on_hours"]); mi=int(round((m["on_hours"]%1)*60))
        mxr=f"{m['max_temp']:.1f} °C" if m["max_temp"] else "—"
        is_cur=(i==len(monthly)-1)
        bg="#FFF8E1" if is_cur else ("#FFF" if i%2==0 else "#F5F5F5")
        bold="font-weight:700;" if is_cur else ""
        rows+=(f'<tr style="background:{bg}">'
               f'<td style="padding:6px 12px;{bold}">{m["label"]}</td>'
               f'<td style="padding:6px 12px;text-align:center;{bold}">{mxr}</td>'
               f'<td style="padding:6px 12px;text-align:center;{bold}">{h}h {mi:02d}m</td>'
               f'<td style="padding:6px 12px;text-align:center">{badge(m.get("delta_temp"))}</td>'
               f'<td style="padding:6px 12px;text-align:center">{badge(m.get("delta_on"))}</td>'
               f'</tr>\n')
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:20px;background:#ECEFF1;font-family:Arial,sans-serif">
<div style="max-width:760px;margin:auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.12)">
<div style="background:linear-gradient(135deg,#1565C0,#0D47A1);padding:26px 30px;color:#fff">
<div style="font-size:22px;font-weight:700">🌡 Boiler Monthly Report</div>
<div style="font-size:14px;margin-top:6px;opacity:.85">{cur_label}</div></div>
<div style="display:flex;border-bottom:1px solid #E0E0E0">
<div style="flex:1;padding:22px 30px;border-right:1px solid #E0E0E0">
<div style="font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:#757575">Max Temperature</div>
<div style="font-size:32px;font-weight:700;color:#1565C0;margin:8px 0 4px">{mx}</div>
<div style="font-size:13px;color:#555">vs {prev_label}: {badge(dt)}</div></div>
<div style="flex:1;padding:22px 30px">
<div style="font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:#757575">Thermostat ON Time</div>
<div style="font-size:32px;font-weight:700;color:#E65100;margin:8px 0 4px">{on_h}h {on_m:02d}m</div>
<div style="font-size:13px;color:#555">vs {prev_label}: {badge(do_)}</div></div></div>
<div style="padding:24px 30px 16px"><div style="font-size:13px;font-weight:600;color:#424242;margin-bottom:10px">Temperature Timeline — {cur_label}</div>
<img src="cid:chart1" style="width:100%;border-radius:8px;display:block"></div>
<div style="padding:8px 30px 16px"><div style="font-size:13px;font-weight:600;color:#424242;margin-bottom:10px">Max Temperature — Last 13 Months</div>
<img src="cid:chart2" style="width:100%;border-radius:8px;display:block"></div>
<div style="padding:8px 30px 16px"><div style="font-size:13px;font-weight:600;color:#424242;margin-bottom:10px">Thermostat ON Time — Last 13 Months</div>
<img src="cid:chart3" style="width:100%;border-radius:8px;display:block"></div>
<div style="padding:8px 30px 28px"><div style="font-size:13px;font-weight:600;color:#424242;margin-bottom:10px">Monthly Breakdown</div>
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead><tr style="background:#1565C0;color:#fff">
<th style="padding:9px 12px;text-align:left">Month</th>
<th style="padding:9px 12px;text-align:center">Max Temp</th>
<th style="padding:9px 12px;text-align:center">ON Time</th>
<th style="padding:9px 12px;text-align:center">Δ Temp</th>
<th style="padding:9px 12px;text-align:center">Δ ON Time</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div style="padding:14px 30px;background:#F5F5F5;text-align:center;font-size:11px;color:#9E9E9E">
Generated by Home Assistant · bnu-homeassistant · {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}</div>
</div></body></html>"""

def main():
    print(f"[boiler_report] Starting {datetime.datetime.now().isoformat()}")
    sec = load_secrets()
    ha_token = sec["boiler_ha_token"]
    smtp_user = sec["boiler_smtp_user"]
    smtp_pass = sec["boiler_smtp_pass"]

    r = requests.get(f"{HA_URL}/api/states/input_text.boiler_report_recipients",
                     headers=_hdrs(ha_token), timeout=10)
    r.raise_for_status()
    recipients = [e.strip() for e in r.json()["state"].split(",") if e.strip()]
    print(f"[boiler_report] Recipients: {recipients}")

    now = datetime.datetime.utcnow()
    months = []
    for i in range(12, -1, -1):
        m = now.month - i; y = now.year
        while m <= 0: m += 12; y -= 1
        months.append((y, m))

    monthly = []
    current_pts = []
    for idx, (y, m) in enumerate(months):
        start, end = month_range(y, m)
        label = start.strftime("%b %Y")
        print(f"[boiler_report] Fetching {label} ...")
        ts = get_history(ha_token, ENTITY_TEMP, start, end)
        ss = get_history(ha_token, ENTITY_SWITCH, start, end)
        max_temp = compute_max_temp(ts)
        on_hours = compute_on_hours(ss)
        if idx == 12: current_pts = get_timed_temps(ts)
        monthly.append({"year":y,"month":m,"label":label,"max_temp":max_temp,
                         "on_hours":on_hours,"delta_temp":None,"delta_on":None})

    delta_temp = pct(monthly[12]["max_temp"], monthly[0]["max_temp"])
    delta_on   = pct(monthly[12]["on_hours"], monthly[0]["on_hours"])
    monthly[12]["delta_temp"] = delta_temp
    monthly[12]["delta_on"]   = delta_on

    cur_label  = monthly[12]["label"]
    labels_13  = [d["label"] for d in monthly]
    max_temps  = [d["max_temp"] for d in monthly]
    on_hrs_13  = [d["on_hours"] for d in monthly]

    print("[boiler_report] Generating charts ...")
    png1 = chart_daily(current_pts, cur_label)
    png2 = chart_bar(labels_13, max_temps, "Max Temperature — Last 13 Months", "°C", C_BLUE)
    png3 = chart_bar(labels_13, on_hrs_13, "Thermostat ON Time — Last 13 Months", "Hours", C_ORANGE)

    os.makedirs(REPORT_DIR, exist_ok=True)
    slug = cur_label.replace(" ", "_")
    for name, data in [("chart1",png1),("chart2",png2),("chart3",png3)]:
        open(f"{REPORT_DIR}/{slug}_{name}.png","wb").write(data)

    html = build_html(cur_label, monthly, delta_temp, delta_on)
    print("[boiler_report] Sending email ...")
    for recipient in recipients:
        msg = MIMEMultipart("related")
        msg["Subject"] = f"Boiler Report — {cur_label}"
        msg["From"] = smtp_user; msg["To"] = recipient
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)
        for cid, data in [("chart1",png1),("chart2",png2),("chart3",png3)]:
            img = MIMEImage(data, _subtype="png")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            msg.attach(img)
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"[boiler_report] ✓ Sent to {recipient}")
    print("[boiler_report] Done.")

if __name__ == "__main__":
    main()
PYEOF
echo "✓ Written /config/scripts/boiler_report.py"

# 5. Install Python dependencies
echo "Installing Python dependencies..."
pip3 install requests pyyaml matplotlib --quiet
echo "✓ Dependencies installed"

# 6. Add secrets placeholders (won't overwrite existing entries)
if ! grep -q "boiler_ha_token" /config/secrets.yaml 2>/dev/null; then
cat >> /config/secrets.yaml << 'SECRETS_EOF'

# Boiler report credentials
boiler_ha_token: "PASTE_YOUR_BNU_LONG_LIVED_TOKEN_HERE"
boiler_smtp_user: "your@gmail.com"
boiler_smtp_pass: "xxxx xxxx xxxx xxxx"
SECRETS_EOF
  echo "✓ Added secret placeholders to /config/secrets.yaml — fill in real values!"
else
  echo "✓ Boiler secrets already present in /config/secrets.yaml"
fi

echo ""
echo "=== Setup complete! Next steps ==="
echo "1. Fill in /config/secrets.yaml with real credentials"
echo "2. Reload HA: Developer Tools → YAML → Reload All"
echo "3. Press 'Send Boiler Report Now' button to test"
