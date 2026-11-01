"""Unit tests for the Joulo plugin helpers. Run: python -m unittest discover -s plugins/Joulo/tests"""
import io
import json
import os
import sys
import unittest
import urllib.error
from datetime import timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import joulo_api  # noqa: E402
import joulo_logic  # noqa: E402

CH = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
CET = timezone(timedelta(hours=1))


def sess(sid, start, end, kwh, ere, eur, charger=CH, status="completed"):
    return {"id": sid, "charger_id": charger, "started_at": start, "ended_at": end, "kwh": kwh, "ere_credits": ere,
            "estimated_euro": eur, "status": status, "counts_for_ere": True, "id_tag": "x", "extra_field": 1}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._raw = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class AggregationTests(unittest.TestCase):
    def test_local_day_uses_timezone(self):
        # 23:30 UTC on the 1st is already the 2nd in CET
        self.assertEqual(joulo_logic.local_day("2026-10-01T23:30:00Z", CET), "2026-10-02")
        self.assertEqual(joulo_logic.local_day("2026-10-01T23:30:00Z", timezone.utc), "2026-10-01")
        self.assertIsNone(joulo_logic.local_day(None))

    def test_aggregate_rounds_once_per_day(self):
        s = [sess("1", "2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z", 10.0004, 3.3, 1.0),
             sess("2", "2026-10-01T18:00:00Z", "2026-10-01T20:00:00Z", 5.0004, 1.7, 0.5),
             sess("3", "2026-10-02T10:00:00Z", None, 2.0, None, None, status="active")]
        days = joulo_logic.aggregate_days(s, CH, timezone.utc)
        self.assertEqual(days["2026-10-01"]["wh"], 15001)
        self.assertEqual(days["2026-10-01"]["cents"], 150)
        self.assertEqual(days["2026-10-01"]["ere_milli"], 5000)
        self.assertEqual(days["2026-10-01"]["count"], 2)
        self.assertEqual(days["2026-10-01"]["seconds"], 3 * 3600)
        self.assertEqual(days["2026-10-02"]["cents"], 0)

    def test_charger_filter(self):
        s = [sess("1", "2026-10-01T10:00:00Z", None, 10, 1, 1), sess("2", "2026-10-01T10:00:00Z", None, 4, 1, 1, charger="other")]
        self.assertEqual(joulo_logic.aggregate_days(s, CH, timezone.utc)["2026-10-01"]["wh"], 10000)
        self.assertEqual(joulo_logic.aggregate_days(s, None, timezone.utc)["2026-10-01"]["wh"], 14000)

    def test_cumulative_is_idempotent(self):
        s = [sess("1", "2026-10-01T10:00:00Z", None, 10, 1, 1), sess("2", "2026-10-03T10:00:00Z", None, 5, 1, 1)]
        rows = joulo_logic.cumulative(joulo_logic.aggregate_days(s, CH, timezone.utc), "wh")
        self.assertEqual(rows, [("2026-10-01", 10000, 10000), ("2026-10-03", 15000, 5000)])
        again = joulo_logic.cumulative(joulo_logic.aggregate_days(list(reversed(s)), CH, timezone.utc), "wh")
        self.assertEqual(rows, again)

    def test_merge_reports_changed_days_and_drops_unknown_fields(self):
        cache = {}
        a = sess("1", "2026-10-01T10:00:00Z", None, 10, 1, 1)
        changed = joulo_logic.merge_sessions(cache, [a])
        self.assertEqual(changed, {joulo_logic.local_day(a["started_at"])})
        self.assertNotIn("extra_field", cache["1"])
        self.assertEqual(joulo_logic.merge_sessions(cache, [a]), set())
        a2 = dict(a, kwh=12)
        self.assertEqual(len(joulo_logic.merge_sessions(cache, [a2])), 1)
        self.assertEqual(cache["1"]["kwh"], 12)

    def test_duration(self):
        s = sess("1", "2026-10-01T10:00:00Z", "2026-10-01T12:15:00Z", 1, 1, 1)
        self.assertEqual(joulo_logic.duration_seconds(s), 8100)


class ClientTests(unittest.TestCase):
    def client(self, handler):
        return joulo_api.JouloClient("secret-token", opener=handler)

    def test_auth_header_and_paging(self):
        seen = []

        def opener(req, timeout=0):
            seen.append(req)
            offset = int(req.full_url.split("offset=")[1].split("&")[0]) if "offset=" in req.full_url else 0
            n = 100 if offset == 0 else 7
            return FakeResponse({"sessions": [{"id": str(offset + i)} for i in range(n)]})

        sessions = self.client(opener).iter_sessions()
        self.assertEqual(len(sessions), 107)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer secret-token")

    def test_errors_never_contain_token(self):
        def opener(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b'{"error": "Missing or invalid API token"}'))

        with self.assertRaises(joulo_api.JouloError) as ctx:
            self.client(opener).get_chargers()
        self.assertTrue(ctx.exception.is_auth)
        self.assertNotIn("secret-token", str(ctx.exception))

    def test_rate_limit_retry_after(self):
        class Headers(dict):
            pass

        def opener(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 429, "x", Headers({"Retry-After": "120"}), io.BytesIO(b"{}"))

        with self.assertRaises(joulo_api.JouloError) as ctx:
            self.client(opener).get_energy()
        self.assertTrue(ctx.exception.is_rate_limited)
        self.assertEqual(ctx.exception.retry_after, 120)

    def test_network_error(self):
        def opener(req, timeout=0):
            raise urllib.error.URLError("boom secret-token")

        with self.assertRaises(joulo_api.JouloError) as ctx:
            self.client(opener).get_energy()
        self.assertEqual(ctx.exception.status, 0)
        self.assertNotIn("secret-token", str(ctx.exception))

    def test_restart_body(self):
        captured = {}

        def opener(req, timeout=0):
            captured["body"] = json.loads(req.data.decode())
            captured["method"] = req.get_method()
            return FakeResponse({"delivered": True, "status": "Accepted"})

        self.client(opener).restart_charger(CH, hard=False)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"], {"charger_id": CH, "type": "Soft"})


if __name__ == "__main__":
    unittest.main()
