import json
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from token_heatmap.ccusage import parse_ccusage_payload
from token_heatmap.db import aggregate_range, connect, fetch_daily_usage, upsert_source_usage
from token_heatmap.server import make_handler


def payload(machine_id="gray-mac", total=100):
    return {
        "machine_id": machine_id,
        "generated_at": datetime.now().astimezone().isoformat(),
        "days": [
            {
                "date": date.today().isoformat(),
                "input_tokens": 20,
                "cached_input_tokens": 70,
                "output_tokens": 10,
                "reasoning_tokens": 3,
                "total_tokens": total,
                "request_count": 0,
                "cost_usd": 1.25,
            }
        ],
    }


class CCUsageTests(unittest.TestCase):
    def test_parse_rejects_duplicate_dates(self):
        value = payload()
        value["days"].append(dict(value["days"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate date"):
            parse_ccusage_payload(value, "Asia/Shanghai")

    def test_snapshot_upsert_is_idempotent_and_can_increase(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "usage.sqlite")
            first = parse_ccusage_payload(payload(total=100), "Asia/Shanghai")
            upsert_source_usage(connection, first)
            upsert_source_usage(connection, first)
            second = parse_ccusage_payload(payload(total=160), "Asia/Shanghai")
            upsert_source_usage(connection, second)
            aggregate_range(connection, date.today(), date.today())
            rows = fetch_daily_usage(connection, date.today(), date.today())
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ccusage_tokens, 160)
        self.assertEqual(rows[0].total_tokens, 160)

    def test_two_machines_are_summed(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "usage.sqlite")
            upsert_source_usage(
                connection,
                parse_ccusage_payload(payload("gray-mac", 100), "Asia/Shanghai"),
            )
            upsert_source_usage(
                connection,
                parse_ccusage_payload(payload("gray-linux", 250), "Asia/Shanghai"),
            )
            aggregate_range(connection, date.today(), date.today())
            row = fetch_daily_usage(connection, date.today(), date.today())[0]
            connection.close()
        self.assertEqual(row.ccusage_tokens, 350)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "usage.sqlite"
        self.token = "x" * 64
        from http.server import ThreadingHTTPServer

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.database, self.token, "Asia/Shanghai"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v1/ccusage"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def _request(self, token):
        request = Request(
            self.url,
            data=json.dumps(payload()).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        return urlopen(request, timeout=5)

    def test_api_requires_bearer_token(self):
        with self.assertRaises(HTTPError) as caught:
            self._request("wrong-token")
        self.assertEqual(caught.exception.code, 401)

    def test_api_upserts_snapshot(self):
        with self._request(self.token) as response:
            body = json.loads(response.read())
        self.assertEqual(body, {"upserted": 1})
        with self._request(self.token) as response:
            self.assertEqual(json.loads(response.read()), {"upserted": 1})
        connection = connect(self.database)
        count = connection.execute("SELECT COUNT(*) FROM source_usage").fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
