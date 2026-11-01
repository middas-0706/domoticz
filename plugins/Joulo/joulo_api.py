"""Minimal client for the Joulo REST API (https://developer.joulo.nl).

Standard library only. The API token is only ever placed in the Authorization header and is never part of an
exception message or log line.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.joulo.nl/functions/v1/api"
PAGE_SIZE = 100


class JouloError(Exception):
    """Any failure talking to Joulo. status is the HTTP status, 0 for network errors."""

    def __init__(self, message, status=0, retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def is_auth(self):
        return self.status in (401, 403)

    @property
    def is_rate_limited(self):
        return self.status == 429


class JouloClient:
    def __init__(self, token, base_url=BASE_URL, timeout=30, opener=None):
        self._token = (token or "").strip()
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._open = opener or urllib.request.urlopen

    def _request(self, method, path, params=None, body=None):
        url = self._base + path
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json", "User-Agent": "Domoticz-Joulo"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as err:
            raw = ""
            try:
                raw = err.read().decode("utf-8")
            except Exception:
                pass
            message = "HTTP %d" % err.code
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("error"):
                    message += ": " + str(payload["error"])
            except ValueError:
                pass
            retry_after = 0
            try:
                retry_after = int(err.headers.get("Retry-After", "0"))
            except (TypeError, ValueError, AttributeError):
                pass
            raise JouloError(message, err.code, retry_after) from None
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            raise JouloError("network error: %s" % type(err).__name__, 0) from None
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            raise JouloError("invalid JSON from Joulo", status) from None
        return status, payload

    def _get(self, path, params=None):
        return self._request("GET", path, params)[1]

    def get_chargers(self):
        return self._get("/chargers").get("chargers", [])

    def get_sessions(self, from_=None, to=None, charger_id=None, limit=PAGE_SIZE, offset=0):
        return self._get("/sessions", {"from": from_, "to": to, "charger_id": charger_id, "limit": limit, "offset": offset})

    def iter_sessions(self, from_=None, to=None, charger_id=None, on_page=None):
        """Page through /sessions and return every session, newest first as delivered by the API."""
        sessions = []
        offset = 0
        while True:
            page = self.get_sessions(from_, to, charger_id, PAGE_SIZE, offset)
            batch = page.get("sessions", [])
            sessions.extend(batch)
            if on_page:
                on_page(len(sessions))
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return sessions

    def get_energy(self):
        return self._get("/energy")

    def get_ere_position(self, year=None):
        return self._get("/ere-position", {"year": year})

    def restart_charger(self, charger_id, hard=False):
        status, payload = self._request("POST", "/chargers/reboot", body={"charger_id": charger_id, "type": "Hard" if hard else "Soft"})
        return status, payload


def iso_now_minus(days):
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
