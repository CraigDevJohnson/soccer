"""Exercise the loopback HTTP boundary without opening the real archive."""

from contextlib import redirect_stderr
import http.client
import io
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.boise_collect import explorer
from scripts.boise_collect.explorer_data import QueryError, RecordNotFound


class FakeArchive:
    def __init__(self):
        self.calls = []
        self.reloads = 0

    def overview(self):
        return {"counts": {"games": 2}, "reloads": self.reloads}

    def coverage(self):
        return {"validated_dates": 1}

    def schema(self):
        return {"tables": ["games"]}

    def list_items(self, kind, params):
        self.calls.append(("list", kind, params))
        if "unknown" in params:
            raise QueryError("Unknown filter")
        return {"items": [{"id": "1"}], "total": 1, "limit": 50, "offset": 0}

    def detail(self, kind, ident):
        self.calls.append(("detail", kind, ident))
        if ident == "999":
            raise RecordNotFound("Record not found")
        if ident == "500":
            raise RuntimeError("private diagnostic text")
        return {"item": {"id": ident}}

    def sql(self, statement):
        self.calls.append(("sql", statement))
        if not statement.startswith("SELECT"):
            raise QueryError("Only read-only SQL is supported")
        return {"columns": ["value"], "rows": [[1]], "truncated": False,
                "row_limit": 200, "elapsed_ms": 1}

    def reload(self):
        self.reloads += 1


class ExplorerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.web = self.root / "web"
        self.web.mkdir()
        for name, content in (("index.html", "<!doctype html><title>Archive</title>"),
                              ("app.css", "body { color: navy; }"),
                              ("app.js", "document.title = 'Archive';"),
                              ("app-icon.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>')):
            (self.web / name).write_text(content)
        self.archive = FakeArchive()
        self.web_patch = patch.object(explorer, "WEB", self.web)
        self.web_patch.start()
        with patch.object(explorer, "Archive", return_value=self.archive) as constructor:
            self.server = explorer.make_server(self.root, port=0)
        constructor.assert_called_once_with(self.root)
        self.port = self.server.server_address[1]
        self.authority = f"127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.web_patch.stop()
        self.temporary.cleanup()

    def request(self, method, target, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            result = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, dict(response.getheaders()), result
        finally:
            connection.close()

    def test_only_loopback_is_bound_and_explicit_static_routes_are_served(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for path, content_type in (("/", "text/html"), ("/app.css", "text/css"),
                                   ("/app.js", "text/javascript"), ("/app-icon.svg", "image/svg+xml")):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(content_type))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                self.assertTrue(body)
        for target in ("/web/", "/../collector.sqlite3", "/%2e%2e/collector.sqlite3", "/index.html", "/favicon.ico"):
            self.assertEqual(self.request("GET", target)[0], 404)

    def test_api_routes_forward_only_supported_kinds_and_single_value_filters(self):
        for path in ("overview", "coverage", "schema"):
            self.assertEqual(self.request("GET", "/api/" + path)[0], 200)
        for kind in ("sessions", "divisions", "teams", "players", "games"):
            self.assertEqual(self.request("GET", f"/api/{kind}?search=Team%20One&limit=20")[0], 200)
            self.assertIn(("list", kind, {"search": "Team One", "limit": "20"}), self.archive.calls)
            self.assertEqual(self.request("GET", f"/api/{kind}/123")[2], {"item": {"id": "123"}})
        for target, expected in (("/api/unknown", 404), ("/api/games/1/extra", 404),
                                 ("/api/games?limit=1&limit=2", 400),
                                 ("/api/games?unknown=x", 400), ("/api/overview?search=x", 400)):
            with self.subTest(target=target):
                self.assertEqual(self.request("GET", target)[0], expected)

    def test_host_is_exact_loopback_authority_including_port(self):
        for host in ("attacker.example", f"127.0.0.1:{self.port + 1}",
                     f"127.0.0.1.attacker.example:{self.port}", f"localhost.:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/api/overview", headers={"Host": host})[0], 403)
        host = f"localhost:{self.port}"
        self.assertEqual(self.request("GET", "/api/overview", headers={"Host": host, "Origin": "http://" + host})[0], 200)
        for hosts in ([], [self.authority, self.authority]):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                connection.putrequest("GET", "/api/overview", skip_host=True)
                for host in hosts:
                    connection.putheader("Host", host)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assertIn("error", json.loads(response.read()))
            finally:
                connection.close()

    def test_api_rejects_cross_origin_and_cross_site_before_archive_access(self):
        for headers in ({"Origin": "https://attacker.example"}, {"Origin": "null"},
                        {"Origin": f"http://localhost:{self.port}"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                status, _, body = self.request("POST", "/api/query", b'{"sql":"SELECT 1"}',
                                               {"Content-Type": "application/json", **headers})
                self.assertEqual(status, 403)
                self.assertIn("error", body)
        self.assertEqual(self.archive.calls, [])
        self.assertEqual(self.request("GET", "/api/overview", headers={"Origin": "http://" + self.authority})[0], 200)

    def test_query_and_reload_use_json_and_the_same_origin_boundary(self):
        status, _, result = self.request("POST", "/api/query", json.dumps({"sql": "SELECT 1"}),
                                         {"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 200)
        self.assertEqual(result["rows"], [[1]])
        self.assertEqual(self.archive.calls[-1], ("sql", "SELECT 1"))
        self.assertEqual(self.request("POST", "/api/query", '{"sql":"DELETE FROM games"}',
                                      {"Content-Type": "application/json"})[0], 400)
        for body, headers in ((None, {}), ("{}", {"Content-Type": "application/json"})):
            self.assertEqual(self.request("POST", "/api/reload", body, headers)[0], 200)
        self.assertEqual(self.archive.reloads, 2)
        self.assertEqual(self.request("POST", "/api/reload", '{"extra":true}',
                                      {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("GET", "/api/query")[0], 405)
        self.assertEqual(self.request("DELETE", "/api/games/1")[0], 405)

    def test_invalid_and_oversized_request_bodies_never_reach_query(self):
        for raw in ("not json", "[]", "null", '{"sql":5}', '{"sql":"SELECT 1","extra":1}',
                    '{"sql":"SELECT 1","sql":"SELECT 2"}', "[" * 1500 + "]" * 1500):
            with self.subTest(raw=raw):
                status, _, body = self.request("POST", "/api/query", raw, {"Content-Type": "application/json"})
                self.assertEqual(status, 400)
                self.assertIn("error", body)
        self.assertEqual(self.request("POST", "/api/query", '{}', {"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request("POST", "/api/query", b"x" * (explorer.MAX_BODY + 1),
                                      {"Content-Type": "application/json"})[0], 413)
        self.assertEqual(self.request("POST", "/api/query", b"", {"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.archive.calls, [])
        self.assertEqual(self.request("GET", "/api/games?search=" + "x" * explorer.MAX_TARGET)[0], 414)

    def test_ambiguous_or_excessive_body_lengths_are_rejected(self):
        for lengths, expected in ((["1", "1"], 400), (["-1"], 400), (["x"], 400),
                                  (["9" * 5000], 413), ([], 400)):
            with self.subTest(lengths=[value[:20] for value in lengths]):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                try:
                    connection.putrequest("POST", "/api/query")
                    connection.putheader("Content-Type", "application/json")
                    for value in lengths:
                        connection.putheader("Content-Length", value)
                    connection.endheaders()
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    self.assertIn("error", json.loads(response.read()))
                finally:
                    connection.close()
        self.assertEqual(self.archive.calls, [])

    def test_response_size_is_bounded(self):
        with patch.object(self.archive, "overview", return_value={"value": "x" * 200}), patch.object(explorer, "MAX_RESPONSE", 100):
            status, _, body = self.request("GET", "/api/overview")
        self.assertEqual(status, 413)
        self.assertIn("error", body)

    def test_error_responses_are_structured_and_requests_are_not_logged(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(self.request("GET", "/api/games/999")[0], 404)
            status, _, body = self.request("GET", "/api/games/500")
        self.assertEqual(status, 500)
        self.assertIn("error", body)
        self.assertNotIn("private diagnostic", json.dumps(body))
        self.assertEqual(stderr.getvalue(), "")

    def test_help_needs_no_database(self):
        result = subprocess.run([sys.executable, "-m", "scripts.boise_collect.explorer", "--help"],
                                cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--data-dir", result.stdout)
        self.assertIn("--port", result.stdout)

    def test_foreground_main_stops_cleanly_on_sigterm(self):
        script = """from unittest.mock import patch
from scripts.boise_collect.explorer import main
with patch('scripts.boise_collect.explorer.Archive'):
    raise SystemExit(main(['--data-dir', '/not-an-archive', '--port', '0']))
"""
        process = subprocess.Popen([sys.executable, "-c", script],
                                   cwd=Path(__file__).resolve().parents[3],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(5), "server did not announce its address")
            address = process.stdout.readline().strip()
            self.assertIn("http://127.0.0.1:", address)
            port = int(address.rsplit(":", 1)[1].rstrip("/"))
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                with self.assertRaises(OSError):
                    connection.connect()
            finally:
                connection.close()
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


if __name__ == "__main__":
    unittest.main()
