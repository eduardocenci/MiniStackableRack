#!/usr/bin/env python3
"""
Boiler Monthly Report Generator
Deploy to: /config/scripts/boiler_report.py

- Reads SMTP credentials + HA token from /config/secrets.yaml (never stored here)
- Queries HA history API for 13 calendar months of boiler data
- Generates 3 matplotlib charts (inline PNG)
- Sends an HTML email with inline charts to all recipients in
  input_text.boiler_report_recipients
"""

import os
import io
import json
import calendar
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from zoneinfo import ZoneInfo

import requests
import yaml
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ── Non-secret config (edit here if your setup differs) ──────────────────────
HA_URL         = "http://localhost:8123"
ENTITY_TEMP    = "sensor.boiler_current_temperature"
ENTITY_SWITCH  = "switch.boiler_termostato"
REPORT_DIR     = "/config/www/boiler_reports"
SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 465   # SSL; use 587 + starttls if preferred

# ── Colour palette ────────────────────────────────────────────────────────────
C_BLUE      = "#1565C0"
C_ORANGE    = "#E65100"
C_HIGHLIGHT = "#F9A825"
C_BG        = "#FAFAFA"


# ─────────────────────────────────────────────────────────────────────────────
# Secrets
# ─────────────────────────────────────────────────────────────────────────────
def load_secrets() -> dict:
    with open("/config/secrets.yaml") as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────────────────────────────────────
# HA history helpers
# ─────────────────────────────────────────────────────────────────────────────
def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def get_history(token: str, entity_id: str, start: datetime.datetime, end: datetime.datetime) -> list:
    url = (
        f"{HA_URL}/api/history/period/{start.strftime('%Y-%m-%dT%H:%M:%S')}"
        f"?end_time={end.strftime('%Y-%m-%dT%H:%M:%S')}"
        f"&filter_entity_id={entity_id}"
        f"&minimal_response=false&no_attributes=true"
    )
    r = requests.get(url, headers=_headers(token), timeout=120)
    r.raise_for_status()
    data = r.json()
    return data[0] if data else []


def month_range(year: int, month: int):
    start = datetime.datetime(year, month, 1, 0, 0, 0)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime.datetime(year, month, last_day, 23, 59, 59)
    return start, end


def compute_max_temp(states: list) -> float | None:
    temps = []
    for s in states:
        try:
            v = float(s["state"])
            if 0 < v < 120:
                temps.append(v)
        except (ValueError, TypeError):
            pass
    return max(temps) if temps else None


def compute_on_hours(states: list) -> float:
    total = 0.0
    prev_time = None
    prev_state = None
    for s in states:
        raw = s.get("last_changed") or s.get("last_updated", "")
        try:
            t = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue
        if prev_state == "on" and prev_time is not None:
            total += (t - prev_time).total_seconds() / 3600
        prev_time = t
        prev_state = s.get("state")
    return total


def get_timed_temps(states: list) -> list[tuple]:
    """Returns [(datetime, float)] for the daily temperature chart."""
    result = []
    for s in states:
        raw = s.get("last_changed") or s.get("last_updated", "")
        try:
            t = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
            v = float(s["state"])
            if 0 < v < 120:
                result.append((t, v))
        except (ValueError, TypeError):
            pass
    return result


def get_on_periods(states: list) -> list[tuple]:
    """Returns [(start_utc, end_utc)] naive-UTC tuples for each ON segment."""
    periods = []
    on_start = None
    for s in states:
        raw = s.get("last_changed") or s.get("last_updated", "")
        try:
            t = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue
        state = s.get("state")
        if state == "on" and on_start is None:
            on_start = t
        elif state != "on" and on_start is not None:
            periods.append((on_start, t))
            on_start = None
    # Close any still-open period at last known timestamp
    if on_start is not None and states:
        raw = states[-1].get("last_changed") or states[-1].get("last_updated", "")
        try:
            t_end = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
            periods.append((on_start, t_end))
        except ValueError:
            pass
    return periods


# ─────────────────────────────────────────────────────────────────────────────
# Chart generators
# ─────────────────────────────────────────────────────────────────────────────
def _fig_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=C_BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def chart_daily_temp(pts: list, month_label: str, target_temp: float | None = None,
                     on_periods: list | None = None) -> bytes:
    fig, ax = plt.subplots(figsize=(9, 3.5), facecolor=C_BG)
    ax.set_facecolor(C_BG)

    if pts:
        times, temps = zip(*pts)

        # Shade thermostat ON periods before the temperature line
        if on_periods:
            for i, (t_start, t_end) in enumerate(on_periods):
                ax.axvspan(t_start, t_end, color=C_ORANGE, alpha=0.12,
                           label="Thermostat ON" if i == 0 else None)

        ax.plot(times, temps, color=C_BLUE, linewidth=1.6, alpha=0.9)
        ax.fill_between(times, temps, alpha=0.08, color=C_BLUE)
        max_v = max(temps)
        max_t = times[list(temps).index(max_v)]
        ax.scatter([max_t], [max_v], color="red", zorder=5, s=55)
        ax.annotate(
            f"  {max_v:.1f} °C",
            xy=(max_t, max_v), fontsize=8.5, color="red", va="bottom"
        )
        if target_temp is not None:
            ax.axhline(target_temp, color=C_ORANGE, linewidth=1.2,
                       linestyle="--", alpha=0.75, label=f"Target {target_temp:.0f} °C")
        if on_periods or target_temp is not None:
            ax.legend(fontsize=8, loc="lower right", framealpha=0.7)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%-d %b"))
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=5))
    else:
        ax.text(0.5, 0.5, "No data available", ha="center", va="center",
                transform=ax.transAxes, color="#aaa", fontsize=11)

    ax.set_title(f"Water Temperature — {month_label}", fontsize=11, pad=8)
    ax.set_ylabel("°C", fontsize=9)
    ax.tick_params(axis="both", labelsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    return _fig_bytes(fig)


def chart_bar_13m(labels: list, values: list, title: str, ylabel: str, color: str) -> bytes:
    fig, ax = plt.subplots(figsize=(9, 3.5), facecolor=C_BG)
    ax.set_facecolor(C_BG)

    colors = [C_HIGHLIGHT if i == len(values) - 1 else color for i in range(len(values))]
    safe_vals = [v if v is not None else 0 for v in values]
    bars = ax.bar(labels, safe_vals, color=colors, width=0.6, zorder=2)

    max_val = max(safe_vals) if safe_vals else 1
    for bar, val in zip(bars, safe_vals):
        if val > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + max_val * 0.015,
                f"{val:.1f}",
                ha="center", va="bottom", fontsize=7.5
            )

    ax.set_title(title, fontsize=11, pad=8)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.tick_params(axis="x", labelsize=7.5, rotation=45)
    ax.tick_params(axis="y", labelsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25, zorder=1)
    fig.tight_layout()
    return _fig_bytes(fig)


# ─────────────────────────────────────────────────────────────────────────────
# HTML email builder
# ─────────────────────────────────────────────────────────────────────────────
def _badge(pct: float | None) -> str:
    if pct is None:
        return '<span style="color:#9E9E9E">N/A</span>'
    color = "#2E7D32" if pct >= 0 else "#C62828"
    arrow = "▲" if pct >= 0 else "▼"
    return f'<span style="color:{color};font-weight:600">{arrow} {abs(pct):.1f}%</span>'


def _pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b * 100


def build_html(month_label: str, monthly: list, delta_temp, delta_on, max_temp_time=None) -> str:
    cur = monthly[-1]

    on_h = int(cur["on_hours"])
    on_m = int(round((cur["on_hours"] % 1) * 60))
    max_str = f"{cur['max_temp']:.1f} °C" if cur["max_temp"] else "—"
    max_time_str = max_temp_time.strftime("%-d %b %Y, %H:%M") if max_temp_time else ""
    prev_label = monthly[0]["label"]

    rows = ""
    for i, m in enumerate(monthly):
        h = int(m["on_hours"])
        mi = int(round((m["on_hours"] % 1) * 60))
        mx = f"{m['max_temp']:.1f} °C" if m["max_temp"] else "—"
        is_cur = i == len(monthly) - 1
        bg = "#FFF8E1" if is_cur else ("#FFFFFF" if i % 2 == 0 else "#F5F5F5")
        bold = "font-weight:700;" if is_cur else ""
        dt = _badge(m.get("delta_temp"))
        do = _badge(m.get("delta_on"))
        rows += (
            f'<tr style="background:{bg}">'
            f'<td style="padding:6px 12px;{bold}">{m["label"]}</td>'
            f'<td style="padding:6px 12px;text-align:center;{bold}">{mx}</td>'
            f'<td style="padding:6px 12px;text-align:center;{bold}">{h}h {mi:02d}m</td>'
            f'<td style="padding:6px 12px;text-align:center">{dt}</td>'
            f'<td style="padding:6px 12px;text-align:center">{do}</td>'
            f"</tr>\n"
        )

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:20px;background:#ECEFF1;font-family:Arial,sans-serif">
<div style="max-width:760px;margin:auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.12)">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1565C0 0%,#0D47A1 100%);padding:26px 30px;color:#fff">
    <div style="font-size:22px;font-weight:700">🌡 Boiler Monthly Report</div>
    <div style="font-size:14px;margin-top:6px;opacity:.85">{month_label}</div>
  </div>

  <!-- KPI row -->
  <div style="display:flex;border-bottom:1px solid #E0E0E0">
    <div style="flex:1;padding:22px 30px;border-right:1px solid #E0E0E0">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:#757575">Max Temperature</div>
      <div style="font-size:32px;font-weight:700;color:#1565C0;margin:8px 0 4px">{max_str}</div>
      {f'<div style="font-size:12px;color:#757575;margin-bottom:4px">{max_time_str}</div>' if max_time_str else ''}
      <div style="font-size:13px;color:#555">vs {prev_label}: {_badge(delta_temp)}</div>
    </div>
    <div style="flex:1;padding:22px 30px">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:#757575">Thermostat ON Time</div>
      <div style="font-size:32px;font-weight:700;color:#E65100;margin:8px 0 4px">{on_h}h {on_m:02d}m</div>
      <div style="font-size:13px;color:#555">vs {prev_label}: {_badge(delta_on)}</div>
    </div>
  </div>

  <!-- Chart 1: daily temp -->
  <div style="padding:24px 30px 16px">
    <div style="font-size:13px;font-weight:600;color:#424242;margin-bottom:10px">Temperature Timeline — {month_label}</div>
    <img src="cid:chart1" style="width:100%;border-radius:8px;display:block">
  </div>

  <!-- Chart 2: 13-month max temp -->
  <div style="padding:8px 30px 16px">
    <div style="font-size:13px;font-weight:600;color:#424242;margin-bottom:10px">Max Temperature — Last 13 Months</div>
    <img src="cid:chart2" style="width:100%;border-radius:8px;display:block">
  </div>

  <!-- Chart 3: 13-month ON time -->
  <div style="padding:8px 30px 16px">
    <div style="font-size:13px;font-weight:600;color:#424242;margin-bottom:10px">Thermostat ON Time — Last 13 Months</div>
    <img src="cid:chart3" style="width:100%;border-radius:8px;display:block">
  </div>

  <!-- Monthly breakdown table -->
  <div style="padding:8px 30px 28px">
    <div style="font-size:13px;font-weight:600;color:#424242;margin-bottom:10px">Monthly Breakdown</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#1565C0;color:#fff">
          <th style="padding:9px 12px;text-align:left;font-weight:600">Month</th>
          <th style="padding:9px 12px;text-align:center;font-weight:600">Max Temp</th>
          <th style="padding:9px 12px;text-align:center;font-weight:600">ON Time</th>
          <th style="padding:9px 12px;text-align:center;font-weight:600">Δ Temp</th>
          <th style="padding:9px 12px;text-align:center;font-weight:600">Δ ON Time</th>
        </tr>
      </thead>
      <tbody>
{rows}      </tbody>
    </table>
  </div>

  <!-- Footer -->
  <div style="padding:14px 30px;background:#F5F5F5;text-align:center;font-size:11px;color:#9E9E9E">
    Generated by Home Assistant · bnu-homeassistant · {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
  </div>

</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print(f"[boiler_report] Starting — {datetime.datetime.now().isoformat()}")

    secrets   = load_secrets()
    ha_token  = secrets["boiler_ha_token"]
    smtp_user = secrets["boiler_smtp_user"]
    smtp_pass = secrets["boiler_smtp_pass"]

    # HA timezone (for displaying local times)
    cfg_r = requests.get(f"{HA_URL}/api/config", headers=_headers(ha_token), timeout=10)
    cfg_r.raise_for_status()
    local_tz = ZoneInfo(cfg_r.json().get("time_zone", "UTC"))

    # Recipients from HA input_text
    r = requests.get(
        f"{HA_URL}/api/states/input_text.boiler_report_recipients",
        headers=_headers(ha_token), timeout=10
    )
    r.raise_for_status()
    recipients = [e.strip() for e in r.json()["state"].split(",") if e.strip()]
    print(f"[boiler_report] Recipients: {recipients}")

    # Target temperature from water_heater
    wh_r = requests.get(
        f"{HA_URL}/api/states/water_heater.boiler_boiler",
        headers=_headers(ha_token), timeout=10
    )
    target_temp = None
    if wh_r.ok:
        target_temp = wh_r.json().get("attributes", {}).get("temperature")

    # Build list of 13 calendar months (oldest first, current last)
    now = datetime.datetime.utcnow()
    months = []
    for i in range(12, -1, -1):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        months.append((y, m))

    # Collect stats for each month
    monthly = []
    current_pts = []
    current_on_periods = []

    for idx, (y, m) in enumerate(months):
        start, end = month_range(y, m)
        label = start.strftime("%b %Y")
        print(f"[boiler_report] Fetching {label} ...")

        temp_states   = get_history(ha_token, ENTITY_TEMP,   start, end)
        switch_states = get_history(ha_token, ENTITY_SWITCH, start, end)

        max_temp = compute_max_temp(temp_states)
        on_hours = compute_on_hours(switch_states)

        if idx == 12:  # current month — also get timed points for daily chart
            current_pts = get_timed_temps(temp_states)
            current_on_periods = get_on_periods(switch_states)

        monthly.append({
            "year": y, "month": m, "label": label,
            "max_temp": max_temp, "on_hours": on_hours,
            "delta_temp": None, "delta_on": None,
        })

    # Deltas: current month vs same month last year
    def pct(a, b):
        return None if (a is None or b is None or b == 0) else (a - b) / b * 100

    delta_temp = pct(monthly[12]["max_temp"], monthly[0]["max_temp"])
    delta_on   = pct(monthly[12]["on_hours"], monthly[0]["on_hours"])
    monthly[12]["delta_temp"] = delta_temp
    monthly[12]["delta_on"]   = delta_on

    # Generate charts
    cur_label   = monthly[12]["label"]
    labels_13   = [d["label"] for d in monthly]
    max_temps   = [d["max_temp"] for d in monthly]
    on_hours_13 = [d["on_hours"] for d in monthly]

    # Convert current_pts and on_periods from naive UTC to local timezone
    def to_local(t):
        return t.replace(tzinfo=datetime.timezone.utc).astimezone(local_tz).replace(tzinfo=None)

    current_pts = [(to_local(t), v) for t, v in current_pts]
    current_on_periods = [(to_local(s), to_local(e)) for s, e in current_on_periods]

    # Datetime of max temperature in current month (local time)
    max_temp_time = None
    if current_pts:
        max_temp_time = max(current_pts, key=lambda x: x[1])[0]

    print("[boiler_report] Generating charts ...")
    png1 = chart_daily_temp(current_pts, cur_label, target_temp, current_on_periods)
    png2 = chart_bar_13m(labels_13, max_temps,   "Max Temperature — Last 13 Months",    "°C",   C_BLUE)
    png3 = chart_bar_13m(labels_13, on_hours_13, "Thermostat ON Time — Last 13 Months", "Hours", C_ORANGE)

    # Save PNGs for reference
    os.makedirs(REPORT_DIR, exist_ok=True)
    slug = cur_label.replace(" ", "_")
    for name, data in [("chart1", png1), ("chart2", png2), ("chart3", png3)]:
        with open(f"{REPORT_DIR}/{slug}_{name}.png", "wb") as f:
            f.write(data)

    # Build & send email
    html = build_html(cur_label, monthly, delta_temp, delta_on, max_temp_time)

    print("[boiler_report] Sending email ...")
    for recipient in recipients:
        msg = MIMEMultipart("related")
        msg["Subject"] = f"Boiler Report — {cur_label}"
        msg["From"]    = smtp_user
        msg["To"]      = recipient

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)

        for cid, png_data in [("chart1", png1), ("chart2", png2), ("chart3", png3)]:
            img = MIMEImage(png_data, _subtype="png")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            msg.attach(img)

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        print(f"[boiler_report] ✓ Sent to {recipient}")

    print("[boiler_report] Done.")


if __name__ == "__main__":
    main()
