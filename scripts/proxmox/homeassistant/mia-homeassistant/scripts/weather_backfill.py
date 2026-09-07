#!/usr/bin/env python3
"""weather_backfill.py - backfill historical outdoor data into Casa MIA InfluxDB.

WHERE THIS RUNS
    On mia-raspberrypi ONLY. The InfluxDB box (192.168.2.15:8086) is reachable
    only from MIA-LAN hosts, and the Pi is the MIA-LAN host that also has WAN
    access to the two source APIs. Do NOT run it on the Claude host or inside
    the HA container - neither can reach 192.168.2.15.

WHAT IT WRITES  (org=casa-mia, bucket=house, precision=s)
    measurement "kmia"       tag station=MIA
        fields tmpc,dwpc,relh,sknt,drct,mslp  (from IEM ASOS, KMIA airport)
    measurement "openmeteo"  (no tags)
        fields shortwave_radiation,direct_radiation,diffuse_radiation,
               temperature_2m,cloudcover  (from Open-Meteo Archive, hourly)

SOURCES
    1) IEM ASOS  https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
    2) Open-Meteo Archive  https://archive-api.open-meteo.com/v1/archive

DESIGN NOTES
    - Standard library only (urllib). No pip installs.
    - Idempotent: line protocol points share (measurement, tag set, timestamp),
      so a re-run overwrites the same series instead of duplicating it.
    - Both sources are fetched one calendar year at a time so a decade of data
      never lands in a single giant request and progress is visible.
    - The InfluxDB token is read from os.environ['MIA_INFLUXDB_TOKEN'] and is
      never written to a file or printed.

USAGE
    export MIA_INFLUXDB_TOKEN='...'          # never hardcode it
    python3 weather_backfill.py                       # 2015-01-01 .. today
    python3 weather_backfill.py 2020-01-01            # 2020-01-01 .. today
    python3 weather_backfill.py 2020-01-01 2020-12-31 # explicit range
"""

import calendar
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
INFLUX_URL = "http://192.168.2.15:8086/api/v2/write"
INFLUX_ORG = "casa-mia"
INFLUX_BUCKET = "house"
INFLUX_PRECISION = "s"

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IEM_STATION = "MIA"
IEM_DATA = ["tmpc", "dwpc", "relh", "sknt", "drct", "mslp"]

OM_URL = "https://archive-api.open-meteo.com/v1/archive"
OM_LAT = 25.7617
OM_LON = -80.1918
OM_HOURLY = [
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "temperature_2m",
    "cloudcover",
]

DEFAULT_START = datetime.date(2015, 1, 1)
BATCH_LINES = 5000          # <= 5000 line-protocol lines per write POST
HTTP_TIMEOUT = 180          # seconds
RETRIES = 3
RETRY_BACKOFF = 5           # seconds, multiplied by attempt number
USER_AGENT = "casa-mia-weather-backfill/1.0 (mia-raspberrypi; urllib)"
MISSING = {"", "M", "m", "None", "null", "NA", "T"}  # T = trace; treated missing


# ----------------------------------------------------------------------------
# Line-protocol helpers
# ----------------------------------------------------------------------------
def esc_meas(s):
    """Escape a measurement name (comma, space)."""
    return s.replace(",", "\\,").replace(" ", "\\ ")


def esc_tag(s):
    """Escape a tag key/value or field key (comma, equals, space)."""
    return s.replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def fmt_float(v):
    """Format a float field value for line protocol (bare number => float)."""
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        raise ValueError("non-finite value")
    return repr(f)


def build_line(measurement, tags, fields, ts):
    """Build one line-protocol line. `fields` must be non-empty (name->float)."""
    head = esc_meas(measurement)
    if tags:
        head += "," + ",".join(
            "%s=%s" % (esc_tag(k), esc_tag(str(v))) for k, v in tags.items()
        )
    body = ",".join("%s=%s" % (esc_tag(k), fmt_float(v)) for k, v in fields.items())
    return "%s %s %d" % (head, body, int(ts))


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------
def http_get(url):
    """GET a URL, returning bytes. Retries on transient failure."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            last = "HTTP %s from %s\n%s" % (e.code, url, detail)
            # 4xx (except 429) are not worth retrying
            if e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = "network error for %s: %s" % (url, e)
        if attempt < RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError("GET failed after %d attempts: %s" % (RETRIES, last))


def influx_write(token, lines):
    """POST a batch of line-protocol lines to InfluxDB. Retries on 5xx/429."""
    if not lines:
        return
    qs = urllib.parse.urlencode(
        {"org": INFLUX_ORG, "bucket": INFLUX_BUCKET, "precision": INFLUX_PRECISION}
    )
    url = "%s?%s" % (INFLUX_URL, qs)
    payload = ("\n".join(lines)).encode("utf-8")
    headers = {
        "Authorization": "Token %s" % token,
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": USER_AGENT,
    }
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                resp.read()
                return  # 204 No Content on success
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:800]
            except Exception:
                pass
            last = "HTTP %s writing to InfluxDB\n%s" % (e.code, detail)
            if e.code == 401:
                raise RuntimeError(
                    "InfluxDB rejected the token (401). Check MIA_INFLUXDB_TOKEN.\n%s"
                    % detail
                )
            if e.code == 404:
                raise RuntimeError(
                    "InfluxDB 404 - org '%s' or bucket '%s' not found.\n%s"
                    % (INFLUX_ORG, INFLUX_BUCKET, detail)
                )
            if e.code < 500 and e.code != 429:
                raise RuntimeError(last)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = ("cannot reach InfluxDB at %s: %s\n"
                    "(are you on mia-raspberrypi? 192.168.2.15 is MIA-LAN only)"
                    % (INFLUX_URL, e))
        if attempt < RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError("InfluxDB write failed after %d attempts: %s" % (RETRIES, last))


class Writer:
    """Buffers line-protocol lines and flushes in <=BATCH_LINES batches."""

    def __init__(self, token):
        self.token = token
        self.buf = []
        self.total = 0

    def add(self, line):
        self.buf.append(line)
        if len(self.buf) >= BATCH_LINES:
            self.flush()

    def flush(self):
        if not self.buf:
            return
        n = len(self.buf)
        influx_write(self.token, self.buf)
        self.total += n
        self.buf = []
        print("    wrote %d points (running total %d)" % (n, self.total), flush=True)


# ----------------------------------------------------------------------------
# Time / range helpers
# ----------------------------------------------------------------------------
def parse_date(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d").date()


def year_chunks(start, end):
    """Yield (chunk_start, chunk_end) date pairs, one per calendar year."""
    cur = start
    while cur <= end:
        chunk_end = datetime.date(cur.year, 12, 31)
        if chunk_end > end:
            chunk_end = end
        yield cur, chunk_end
        cur = datetime.date(cur.year + 1, 1, 1)


def epoch_utc(dt):
    """UTC datetime (naive, assumed UTC) -> epoch seconds."""
    return calendar.timegm(dt.timetuple())


# ----------------------------------------------------------------------------
# Source 1: IEM ASOS -> measurement "kmia"
# ----------------------------------------------------------------------------
def backfill_iem(writer, start, end):
    print("== IEM ASOS (KMIA) %s .. %s ==" % (start, end), flush=True)
    for cs, ce in year_chunks(start, end):
        # IEM's date range is start-inclusive / end-exclusive; extend by one day
        # so the final day of the chunk is fetched. Overlap at the year boundary
        # just overwrites idempotently.
        ce_excl = ce + datetime.timedelta(days=1)
        params = [
            ("station", IEM_STATION),
            ("tz", "Etc/UTC"),
            ("format", "onlycomma"),
            ("latlon", "no"),
            ("missing", "M"),
            ("trace", "T"),
            ("year1", cs.year), ("month1", cs.month), ("day1", cs.day),
            ("year2", ce_excl.year), ("month2", ce_excl.month), ("day2", ce_excl.day),
        ]
        for d in IEM_DATA:
            params.append(("data", d))
        url = IEM_URL + "?" + urllib.parse.urlencode(params)
        print("  fetching %d ..." % cs.year, flush=True)
        raw = http_get(url).decode("utf-8", "replace")

        lines = raw.splitlines()
        if not lines:
            print("    (empty response)", flush=True)
            continue
        header = [h.strip() for h in lines[0].split(",")]
        try:
            idx = {name: header.index(name) for name in (["station", "valid"] + IEM_DATA)}
        except ValueError:
            print("    unexpected header: %r - skipping chunk" % header, flush=True)
            continue

        rows = 0
        for line in lines[1:]:
            if not line.strip():
                continue
            cols = line.split(",")
            if len(cols) < len(header):
                continue
            valid = cols[idx["valid"]].strip()
            if valid in MISSING:
                continue
            try:
                dt = datetime.datetime.strptime(valid, "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            ts = epoch_utc(dt)
            fields = {}
            for name in IEM_DATA:
                v = cols[idx[name]].strip()
                if v in MISSING:
                    continue
                try:
                    fields[name] = float(v)
                except ValueError:
                    continue
            if not fields:
                continue
            writer.add(build_line("kmia", {"station": IEM_STATION}, fields, ts))
            rows += 1
        print("    parsed %d observations" % rows, flush=True)
    writer.flush()


# ----------------------------------------------------------------------------
# Source 2: Open-Meteo Archive -> measurement "openmeteo"
# ----------------------------------------------------------------------------
def backfill_openmeteo(writer, start, end):
    print("== Open-Meteo Archive %s .. %s ==" % (start, end), flush=True)
    for cs, ce in year_chunks(start, end):
        params = {
            "latitude": OM_LAT,
            "longitude": OM_LON,
            "start_date": cs.isoformat(),
            "end_date": ce.isoformat(),
            "hourly": ",".join(OM_HOURLY),
            "timezone": "UTC",
        }
        url = OM_URL + "?" + urllib.parse.urlencode(params)
        print("  fetching %d ..." % cs.year, flush=True)
        raw = http_get(url)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            raise RuntimeError("Open-Meteo returned non-JSON for %d: %s" % (cs.year, e))
        if "error" in data and data.get("error"):
            raise RuntimeError("Open-Meteo error for %d: %s"
                               % (cs.year, data.get("reason", "unknown")))

        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        var_names = [k for k in hourly.keys() if k != "time"]
        rows = 0
        for i, t in enumerate(times):
            try:
                dt = datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M")
            except ValueError:
                continue
            ts = epoch_utc(dt)
            fields = {}
            for name in var_names:
                series = hourly.get(name) or []
                if i >= len(series):
                    continue
                v = series[i]
                if v is None:
                    continue
                try:
                    fields[name] = float(v)
                except (TypeError, ValueError):
                    continue
            if not fields:
                continue
            writer.add(build_line("openmeteo", None, fields, ts))
            rows += 1
        print("    parsed %d hourly rows" % rows, flush=True)
    writer.flush()


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main(argv):
    token = os.environ.get("MIA_INFLUXDB_TOKEN")
    if not token:
        sys.exit(
            "ERROR: MIA_INFLUXDB_TOKEN is not set in the environment.\n"
            "Set it before running, e.g.:\n"
            "  export MIA_INFLUXDB_TOKEN='<token from repo-root .env>'\n"
            "The token must never be hardcoded in this script or any file."
        )

    today = datetime.date.today()
    try:
        start = parse_date(argv[1]) if len(argv) > 1 else DEFAULT_START
        end = parse_date(argv[2]) if len(argv) > 2 else today
    except ValueError:
        sys.exit("ERROR: dates must be YYYY-MM-DD. Usage: weather_backfill.py "
                 "[start YYYY-MM-DD] [end YYYY-MM-DD]")
    if start > end:
        sys.exit("ERROR: start date %s is after end date %s" % (start, end))

    print("Backfilling outdoor data into InfluxDB %s (org=%s bucket=%s)"
          % (INFLUX_URL, INFLUX_ORG, INFLUX_BUCKET), flush=True)
    print("Range: %s .. %s\n" % (start, end), flush=True)

    writer = Writer(token)
    t0 = time.time()
    try:
        backfill_iem(writer, start, end)
        print("", flush=True)
        backfill_openmeteo(writer, start, end)
        writer.flush()
    except RuntimeError as e:
        # Flush whatever succeeded so a partial run is not wasted, then report.
        try:
            writer.flush()
        except Exception:
            pass
        sys.exit("\nFATAL: %s\n(wrote %d points before failing)" % (e, writer.total))

    dt = time.time() - t0
    print("\nDONE. Wrote %d points total in %.1fs." % (writer.total, dt), flush=True)


if __name__ == "__main__":
    main(sys.argv)
