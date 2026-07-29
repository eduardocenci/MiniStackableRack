#!/usr/bin/env python3
"""Condfy portal client + pt-BR message parsing for condfy-bridge.

Facts discovered from an authenticated session — do NOT rediscover these:

  - base URL          https://api.condfy.com.br/api/cwa
  - feed              GET {base}/v1/user/notifications?page=0&size=15
  - auth              a SESSION COOKIE, not a Bearer header. POST
                      /v1/public/auth/login {username, password, deviceUuid}
                      returns 200 with the user profile and NO token in the body;
                      the credential is `Set-Cookie: csl=<jwt>`. requests.Session
                      carries it automatically from then on.
  - edge              a bare python-requests call is answered with an nginx 403
                      HTML page before reaching the app — browser-like headers
                      (see BROWSER_HEADERS) are required
  - scope             the ACCOUNT, not a unit. licenseId 9358 == Céu Azul
  - envelope          {links, page, size, total, first, last, content[]}
  - item              {id, date, title, message, typeName, licenseId, resourceId,
                       open, read}
  - id                a stable integer — it IS the dedup key, no hashing needed
  - date              "YYYY-MM-DD HH:MM", America/Sao_Paulo, no seconds, no offset
  - retention         a short rolling window (~11 items / ~4 days), so history has
                      to be persisted locally

Access events carry typeName CONTROLE_ACESSOS and a rendered pt-BR sentence:
"Altair Dalpra passou por portão grande utilizando tag".

The login route was never captured (it lives in a lazily-loaded JS chunk), so
`login()` probes a short candidate list and remembers whichever works — a 404/405
means "wrong route, try the next", a 401 means "right route, bad credentials".

Run the doctests with:  python -m doctest condfy.py -v
"""
import base64
import json
import logging
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

TZ = ZoneInfo("America/Sao_Paulo")
ACCESS_TYPE = "CONTROLE_ACESSOS"
DEFAULT_BASE_URL = "https://api.condfy.com.br/api/cwa"
WEB_ORIGIN = "https://web.condfy.com.br"
SESSION_COOKIE = "csl"
COOKIE_DOMAIN = ".condfy.com.br"

# The edge in front of api.condfy.com.br answers a bare python-requests call with
# an nginx "403 Forbidden" HTML page — it never reaches the application. Sending
# the same headers the portal's own front-end sends gets a real JSON response.
BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": WEB_ORIGIN,
    "Referer": WEB_ORIGIN + "/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
}

# Recovered from the SPA bundle (assets/main.10c44834.js):
#   POST /v1/public/auth/login {username, password, userId, deviceUuid,
#                               challengeToken, defaultUser?, twoFactorCode?,
#                               authenticationCode?}
# Siblings: /v1/public/auth/logout, /v1/public/auth/refreshToken {userId, deviceUuid}.
# The rest of the list is kept only as a fallback if Condfy ever moves the route.
LOGIN_PATHS = ["/v1/public/auth/login", "/v1/auth/login", "/v1/login",
               "/v1/user/login", "/v1/users/login"]
LOGIN_BODIES = [("username", "password"), ("email", "password"),
                ("login", "senha"), ("email", "senha")]

ACCESS_RE = re.compile(
    r"^(?P<person>.+?)\s+passou\s+por\s+(?P<gate>.+?)"
    r"(?:\s+utilizando\s+(?P<method>.+?))?\s*$",
    re.IGNORECASE,
)
JWT_RE = re.compile(r"^ey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.")


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def normalize(s):
    """Accent- and case-insensitive form used for all matching.

    >>> normalize("Ênio  FAQUETI")
    'enio faqueti'
    >>> normalize(None)
    ''
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.casefold().split())


def slug(s):
    """Topic- and entity-safe identifier.

    >>> slug("portão grande")
    'portao_grande'
    >>> slug("Altair Dalpra")
    'altair_dalpra'
    """
    return re.sub(r"[^a-z0-9]+", "_", normalize(s)).strip("_")


def parse_message(message):
    """Split the rendered sentence into (person, gate, method).

    >>> parse_message("Altair Dalpra passou por portão grande utilizando tag")
    ('Altair Dalpra', 'portão grande', 'tag')
    >>> parse_message("Enio Faqueti passou por portão pequeno")
    ('Enio Faqueti', 'portão pequeno', None)
    >>> parse_message("Altair Dalpra teve acesso negado")
    (None, None, None)
    """
    m = ACCESS_RE.match((message or "").strip())
    if not m:
        return None, None, None
    g = m.groupdict()
    method = g["method"].strip() if g["method"] else None
    return g["person"].strip(), g["gate"].strip(), method


def matches_watch(watch_names, person, message):
    """Which watched name this event belongs to, if any.

    Matches when every token of the watched name is present, so "Enio Faqueti"
    survives accents and middle initials but never collides with a different
    Faqueti. When the sentence did not parse, the whole message is the haystack.

    >>> matches_watch(["Enio Faqueti"], "Ênio Faqueti", "")
    'Enio Faqueti'
    >>> matches_watch(["Enio Faqueti"], "ENIO F. FAQUETI", "")
    'Enio Faqueti'
    >>> matches_watch(["Enio Faqueti"], "Leandro Faqueti", "")
    >>> matches_watch(["Enio Faqueti"], None, "Visitante autorizado por Enio Faqueti")
    'Enio Faqueti'
    """
    hay = set((normalize(person) if person else normalize(message)).split())
    for name in watch_names:
        tokens = normalize(name).split()
        if tokens and all(t in hay for t in tokens):
            return name
    return None


def parse_event_date(raw):
    """'YYYY-MM-DD HH:MM' (local) -> (iso_with_offset, utc_epoch).

    The API gives minute precision with no timezone; America/Sao_Paulo is the
    condo's zone and Brazil has had no DST since 2019.

    >>> parse_event_date("2026-07-28 15:56")
    ('2026-07-28T15:56:00-03:00', 1785264960)
    """
    dt = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ, fold=0)
    return dt.isoformat(), int(dt.timestamp())


def jwt_exp(token):
    """Best-effort `exp` from a JWT payload. No signature verification.

    >>> jwt_exp("not.a.jwt") is None
    True
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def _find_jwt(obj, depth=0):
    """First JWT-looking string anywhere in a decoded JSON body.

    The login response shape is unknown, so this beats guessing a key path.

    >>> _find_jwt({"data": {"accessToken": "eyJhbGciOi.eyJzdWIi.sig"}})
    'eyJhbGciOi.eyJzdWIi.sig'
    >>> _find_jwt({"nope": 1}) is None
    True
    """
    if depth > 6:
        return None
    if isinstance(obj, str):
        return obj if JWT_RE.match(obj) else None
    if isinstance(obj, dict):
        for value in obj.values():
            found = _find_jwt(value, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_jwt(value, depth + 1)
            if found:
                return found
    return None


class LoginError(RuntimeError):
    """Login failed for a reason retrying will not fix (bad credentials, no route)."""


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class CondfyClient:
    """Bearer-authenticated Condfy API client with a self-healing token.

    `on_token` is called with (token, login_path) whenever a new one is obtained,
    so the caller can persist both and skip the probing next time.
    """

    def __init__(self, base_url, email, password, token=None, login_path=None,
                 on_token=None, device_uuid=None, timeout=(5, 20)):
        self.base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.email = email          # Condfy calls this `username`; it is the e-mail
        self.password = password
        # Stable per-install id: a fresh one on every login would pile up entries
        # under the account's "dispositivos conectados".
        self.device_uuid = device_uuid
        # Learned at login; enables the cheap refresh path below. Not persisted —
        # a restart just does one full login.
        self.user_id = None
        self.token = token or None
        self.login_path = login_path or None
        self.on_token = on_token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        if self.token:
            self._install_cookie(self.token)

    def _install_cookie(self, token):
        """Put a (possibly restored) session cookie back on the requests session."""
        self._clear_cookie()
        self.session.cookies.set(SESSION_COOKIE, token,
                                 domain=COOKIE_DOMAIN, path="/")

    def _clear_cookie(self):
        """Drop every session cookie, whatever domain/path it was stored under."""
        for c in list(self.session.cookies):
            if c.name == SESSION_COOKIE:
                try:
                    self.session.cookies.clear(c.domain, c.path, c.name)
                except KeyError:
                    pass

    def _read_cookie(self):
        """Newest session cookie value, or None.

        Not cookies.get(): a restored cookie on `.condfy.com.br` and the server's
        own on `api.condfy.com.br` coexist, and get() raises CookieConflictError
        when a name appears twice.
        """
        values = [c.value for c in self.session.cookies if c.name == SESSION_COOKIE]
        return values[-1] if values else None

    # -- auth ------------------------------------------------------------- #
    @property
    def token_exp(self):
        return jwt_exp(self.token) if self.token else None

    def _candidate_paths(self):
        seen, out = set(), []
        for path in ([self.login_path] if self.login_path else []) + LOGIN_PATHS:
            if path and path not in seen:
                seen.add(path)
                out.append(path)
        return out

    def login(self):
        """Obtain a fresh token, probing routes/body shapes until one works."""
        if not self.email or not self.password:
            raise LoginError("CONDFY_EMAIL/CONDFY_PASSWORD not set")
        self._clear_cookie()          # start clean so the server's cookie is the only one
        unauthorized = []
        for path in self._candidate_paths():
            for user_field, pass_field in LOGIN_BODIES:
                body = {user_field: self.email, pass_field: self.password}
                if self.device_uuid:
                    body["deviceUuid"] = self.device_uuid
                try:
                    r = self.session.post(f"{self.base}{path}", json=body,
                                          timeout=self.timeout)
                except requests.RequestException as exc:
                    raise LoginError(f"{type(exc).__name__} contacting {path}") from exc
                if r.status_code in (404, 405):
                    break                      # wrong route — next path
                if r.status_code in (400, 422):
                    continue                   # right route, wrong field names
                if r.status_code in (401, 403):
                    # Either the credentials are wrong or this path simply needs
                    # auth — keep the detail, it is the only clue for 2FA and
                    # challengeToken flows.
                    unauthorized.append((path, r.text[:200]))
                    break
                if r.ok:
                    # The credential is the session cookie; the body carries only
                    # the user profile. _find_jwt is a fallback in case Condfy
                    # ever moves to a body token.
                    token = self._read_cookie() or _find_jwt(_safe_json(r))
                    if not token:
                        log.warning("%s returned HTTP %s but set no %s cookie",
                                    path, r.status_code, SESSION_COOKIE)
                        continue
                    self.token = token
                    self.login_path = path
                    self.user_id = ((_safe_json(r).get("user") or {}).get("id"))
                    if self.on_token:
                        self.on_token(token, path)
                    exp = self.token_exp
                    log.info("login ok via %s (exp=%s)", path,
                             datetime.fromtimestamp(exp, TZ).isoformat() if exp else "unknown")
                    return token
        if unauthorized:
            path, detail = unauthorized[0]
            raise LoginError(f"credentials rejected at {path}: {detail}")
        raise LoginError("no working login route found — capture it in DevTools "
                         "and set CONDFY_LOGIN_PATH")

    def refresh(self):
        """Renew the session cookie without a full login. True if it worked.

        The cookie lives only ~10 minutes, so this is the difference between one
        login per restart and roughly 288 a day against the endpoint the WAF sits
        in front of (and the account has auditing on).
        """
        if not self.user_id:
            return False
        try:
            r = self.session.post(
                f"{self.base}/v1/public/auth/refreshToken",
                json={"userId": self.user_id, "deviceUuid": self.device_uuid},
                timeout=self.timeout)
        except requests.RequestException as exc:
            log.info("refresh failed (%s) — falling back to login", type(exc).__name__)
            return False
        token = self._read_cookie() if r.ok else None
        if not token:
            log.info("refresh returned HTTP %s — falling back to login", r.status_code)
            return False
        self.token = token
        if self.on_token:
            self.on_token(token, self.login_path)
        exp = self.token_exp
        log.info("session refreshed (exp=%s)",
                 datetime.fromtimestamp(exp, TZ).isoformat() if exp else "unknown")
        return True

    def ensure_token(self, skew=120):
        """Keep a live session: refresh when near expiry, log in when that fails."""
        exp = self.token_exp
        if not self.token:
            self.login()
        elif exp and exp - skew <= _now():
            if not self.refresh():
                self.login()
        return self.token

    # -- requests --------------------------------------------------------- #
    def _get(self, path, params=None, _retried=False):
        # No Authorization header: the session cookie is the credential.
        self.ensure_token()
        r = self.session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        if r.status_code in (401, 403) and not _retried:
            log.info("session rejected (HTTP %s) — re-logging in", r.status_code)
            self.token = None
            self._clear_cookie()
            return self._get(path, params, _retried=True)
        return r

    def notifications(self, page=0, size=15):
        """One page of the account's notification feed. Returns (response, dict)."""
        r = self._get("/v1/user/notifications", {"page": page, "size": size})
        return r, (_safe_json(r) if r.ok else {})


def _safe_json(response):
    try:
        return response.json() or {}
    except ValueError:
        return {}


def _now():
    import time
    return int(time.time())


if __name__ == "__main__":
    import doctest
    print(doctest.testmod())
