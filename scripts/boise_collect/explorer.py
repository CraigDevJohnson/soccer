"""Serve a read-only archive explorer on the local loopback interface."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import sys
from urllib.parse import parse_qsl, urlsplit

from .explorer_data import Archive, QueryError, RecordNotFound


WEB = Path(__file__).with_name("web")
MAX_BODY = 20_000
MAX_TARGET = 8_192
MAX_RESPONSE = 8 * 1024 * 1024
KINDS = frozenset({"sessions", "divisions", "teams", "players", "games"})
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app-icon.svg": ("app-icon.svg", "image/svg+xml")}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "font-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'none'")


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


class ExplorerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, archive):
        self.archive = archive
        super().__init__(address, ExplorerHandler)


class ExplorerHandler(BaseHTTPRequestHandler):
    server_version = "BoiseArchive/1.0"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Request targets and SQL may contain private search text.
        pass

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": HTTPStatus(code).phrase})

    def _send(self, status, body: bytes, content_type: str):
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _json(self, status, value):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_RESPONSE:
            status = 413
            encoded = b'{"error":"Response is too large; narrow the query or filters."}'
        self._send(status, encoded, "application/json; charset=utf-8")

    def _target(self):
        if len(self.path) > MAX_TARGET:
            raise RequestError(414, "Request target is too long.")
        try:
            target = urlsplit(self.path)
            if target.scheme or target.netloc or target.fragment or not target.path.startswith("/"):
                raise ValueError
            pairs = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=16)
        except ValueError:
            raise RequestError(400, "Invalid request target or query parameters.") from None
        params = {}
        for key, value in pairs:
            if not key or key in params or len(key) > 40 or len(value) > 2048:
                raise RequestError(400, "Query parameters must be bounded and single-valued.")
            params[key] = value
        return target.path, params

    def _check_access(self, path):
        port = self.server.server_address[1]
        authorities = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            authorities.update(("127.0.0.1", "localhost"))
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in authorities:
            raise RequestError(403, "Use the local explorer address.")
        if path == "/api" or path.startswith("/api/"):
            origins = self.headers.get_all("Origin", [])
            sites = self.headers.get_all("Sec-Fetch-Site", [])
            if (len(origins) > 1 or (origins and origins[0] != "http://" + hosts[0])
                    or len(sites) > 1 or (sites and sites[0] not in {"same-origin", "same-site", "none"})):
                raise RequestError(403, "Cross-origin archive requests are not allowed.")

    def _body(self, *, optional=False):
        if self.headers.get_all("Transfer-Encoding"):
            raise RequestError(400, "Transfer encoding is not supported.")
        lengths = self.headers.get_all("Content-Length", [])
        if optional and not lengths:
            return {}
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise RequestError(400, "A single valid Content-Length is required.")
        if len(lengths[0]) > 8:
            raise RequestError(413, "Request body exceeds 20,000 bytes.")
        length = int(lengths[0])
        if length > MAX_BODY:
            raise RequestError(413, "Request body exceeds 20,000 bytes.")
        if optional and length == 0:
            return {}
        if len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get_content_type() != "application/json":
            raise RequestError(400, "Content-Type must be application/json.")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (ValueError, RecursionError, TimeoutError):
            raise RequestError(400, "Request body must contain complete UTF-8 JSON.") from None
        if not isinstance(value, dict):
            raise RequestError(400, "Request body must be a JSON object.")
        return value

    def _route(self, path, params):
        archive = self.server.archive
        if self.command == "GET" and path in STATIC:
            filename, content_type = STATIC[path]
            file = WEB / filename
            if file.is_symlink() or not file.is_file():
                raise RequestError(404, "Static file not found.")
            if file.stat().st_size > MAX_RESPONSE:
                raise RequestError(413, "Static file is too large.")
            self._send(200, file.read_bytes(), content_type)
            return
        parts = path.split("/")
        list_route = len(parts) == 3 and parts[1] == "api" and parts[2] in KINDS
        if params and not list_route:
            raise RequestError(400, "This endpoint does not accept query parameters.")
        if self.command == "POST" and path in {"/api/query", "/api/reload"}:
            value = self._body(optional=path == "/api/reload")
            if path == "/api/query":
                if set(value) != {"sql"} or not isinstance(value["sql"], str):
                    raise RequestError(400, "Provide exactly one sql string.")
                result = archive.sql(value["sql"])
            else:
                if value:
                    raise RequestError(400, "Reload accepts an empty body or JSON object only.")
                archive.reload()
                result = archive.overview()
            self._json(200, result)
            return
        if self.command != "GET" or path in {"/api/query", "/api/reload"}:
            raise RequestError(405, "Method not allowed.")
        if path == "/api/overview":
            result = archive.overview()
        elif path == "/api/coverage":
            result = archive.coverage()
        elif path == "/api/schema":
            result = archive.schema()
        elif list_route:
            result = archive.list_items(parts[2], params)
        elif len(parts) == 4 and parts[1] == "api" and parts[2] in KINDS and parts[3]:
            result = archive.detail(parts[2], parts[3])
        else:
            raise RequestError(404, "Endpoint not found.")
        self._json(200, result)

    def _dispatch(self):
        try:
            path, params = self._target()
            self._check_access(path)
            self._route(path, params)
        except RequestError as error:
            self._json(error.status, {"error": str(error)})
        except RecordNotFound as error:
            self._json(404, {"error": str(error)})
        except QueryError as error:
            self._json(400, {"error": str(error)})
        except Exception:
            self._json(500, {"error": "The local archive could not complete this request."})

    do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = _dispatch


def make_server(root: Path, port: int = 0) -> ExplorerServer:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    return ExplorerServer(("127.0.0.1", port), Archive(Path(root)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Browse the local Boise archive without changing or collecting data")
    parser.add_argument("--data-dir", type=Path, required=True, help="existing archive directory")
    parser.add_argument("--port", type=int, default=8767, help="loopback port (default 8767; 0 selects an available port)")
    args = parser.parse_args(argv)

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    try:
        with make_server(args.data_dir, args.port) as server:
            previous_handler = signal.signal(signal.SIGTERM, interrupt)
            try:
                print(f"Boise archive explorer: http://127.0.0.1:{server.server_address[1]}/", flush=True)
                server.serve_forever(poll_interval=0.2)
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        print(f"Explorer stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
