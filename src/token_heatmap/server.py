import argparse
import hmac
import json
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Type

from .ccusage import parse_ccusage_payload
from .db import connect, upsert_source_usage


MAX_BODY_BYTES = 1024 * 1024
LOGGER = logging.getLogger("token_heatmap.server")


def _read_token(path: Path) -> str:
    token = Path(path).read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("ingest token must contain at least 32 characters")
    return token


def make_handler(
    database_path: Path, ingest_token: str, timezone_name: str
) -> Type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "token-heatmap"

        def _write_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not header.startswith(prefix):
                return False
            return hmac.compare_digest(header[len(prefix) :], ingest_token)

        def do_GET(self) -> None:
            if self.path != "/healthz":
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._write_json(HTTPStatus.OK, {"status": "ok"})

        def do_POST(self) -> None:
            if self.path != "/api/v1/ccusage":
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not self._authorized():
                self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return

            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "")
            except ValueError:
                self._write_json(
                    HTTPStatus.LENGTH_REQUIRED, {"error": "content_length_required"}
                )
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._write_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid_body_size"}
                )
                return

            try:
                payload = json.loads(self.rfile.read(length))
                usages = parse_ccusage_payload(payload, timezone_name)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_payload", "detail": str(error)},
                )
                return

            connection = connect(database_path)
            try:
                count = upsert_source_usage(connection, usages)
            finally:
                connection.close()
            self._write_json(HTTPStatus.OK, {"upserted": count})

        def log_message(self, message: str, *args: Any) -> None:
            LOGGER.info("%s - %s", self.client_address[0], message % args)

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Receive ccusage snapshots")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("TOKEN_HEATMAP_DB_PATH", "data/token_usage.sqlite")),
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(
            os.environ.get(
                "TOKEN_HEATMAP_INGEST_TOKEN_FILE",
                "/etc/token-heatmap/ingest-token",
            )
        ),
    )
    parser.add_argument(
        "--timezone",
        default=os.environ.get("TOKEN_HEATMAP_TIMEZONE", "Asia/Shanghai"),
    )
    parser.add_argument(
        "--host", default=os.environ.get("TOKEN_HEATMAP_LISTEN_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TOKEN_HEATMAP_LISTEN_PORT", "8765")),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = _read_token(args.token_file)
    connection = connect(args.database)
    connection.close()
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(args.database, token, args.timezone)
    )
    LOGGER.info("listening on %s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
