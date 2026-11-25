from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from scripts.boise_collect.bruno_client import BrunoClient, FRAME, _capture, _decode
from scripts.boise_collect.collector import MAX_BODY, FetchError, request_path
from scripts.boise_collect.store import Store
from scripts.boise_collect.tests.test_collector import FixtureServer


REQUEST_ID = "a" * 32


def frame(**changes):
    result = {"version": 1, "requestId": REQUEST_ID, "status": 200,
              "retryAfter": None, "bodyBase64": "e30="}
    result.update(changes)
    return FRAME + json.dumps(result).encode() + b"\n"


class ResultContractTests(unittest.TestCase):
    def test_preserves_original_bytes(self):
        raw = b' {"big":123456789012345678901234567890,"escape":"\\u00e9"}\n'
        response = _decode(frame(bodyBase64=base64.b64encode(raw).decode()), 0, REQUEST_ID)
        self.assertEqual(response.body, raw)

    def test_requires_exactly_one_valid_matching_result(self):
        for output in (b"", b"ordinary CLI output\n", FRAME + b"not JSON\n", frame() + frame(),
                       frame(requestId="wrong"), frame(version=2), frame(version=True), frame(status=True),
                       frame(status="200"), frame(status=999), frame(retryAfter=[]),
                       frame(bodyBase64="!"), frame(bodyBase64=5), frame(unexpected="value")):
            with self.subTest(output=output[:80]), self.assertRaises(FetchError) as caught:
                _decode(output, 0, REQUEST_ID)
            self.assertEqual(str(caught.exception), "bruno_invalid_result")

    def test_failed_subprocess_cannot_commit_a_success_frame(self):
        with self.assertRaises(FetchError) as caught:
            _decode(frame(status=200, retryAfter="60"), 1, REQUEST_ID)
        self.assertEqual(str(caught.exception), "bruno_process_failed")
        self.assertEqual(caught.exception.status, 200)
        self.assertEqual(caught.exception.retry_after, "60")

    def test_transport_error_preserves_http_boundary(self):
        result = {"version": 1, "requestId": REQUEST_ID, "status": 429,
                  "retryAfter": "60", "error": "body_too_large"}
        with self.assertRaises(FetchError) as caught:
            _decode(FRAME + json.dumps(result).encode(), 1, REQUEST_ID)
        self.assertEqual(str(caught.exception), "body_too_large")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, "60")

    def test_parent_enforces_body_limit(self):
        with patch("scripts.boise_collect.bruno_client.MAX_BODY", 1):
            with self.assertRaises(FetchError) as caught:
                _decode(frame(), 0, REQUEST_ID)
        self.assertEqual(str(caught.exception), "body_too_large")

    def test_requires_pinned_local_dependency(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("scripts.boise_collect.bruno_client.COLLECTION", Path(folder)):
                with self.assertRaisesRegex(ValueError, "collector:setup"):
                    BrunoClient("http://127.0.0.1:12345")


class TransportBodyTests(unittest.TestCase):
    def test_large_valid_responses_preserve_every_byte(self):
        for size in (100 * 1024, 1024 * 1024, MAX_BODY - 1):
            with self.subTest(size=size):
                body = b'{"padding":"' + b'a' * (size - 14) + b'"}'
                routes = {request_path("division", "11"): (
                    200, body, {"Content-Type": "application/json"},
                )}
                with FixtureServer(routes) as server:
                    response = BrunoClient(server.origin).get("division", "11")
                    self.assertEqual(len(server.calls), 1)
                self.assertEqual(response.status, 200)
                self.assertEqual(len(response.body), size)
                self.assertEqual(hashlib.sha256(response.body).digest(), hashlib.sha256(body).digest())


class ProcessContractTests(unittest.TestCase):
    def process(self, code):
        return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)

    def test_captures_only_bounded_output(self):
        process = self.process("import os; os.write(1, b'x'*2000)")
        with self.assertRaises(FetchError) as caught:
            _capture(process, 5, 1000)
        self.assertEqual(str(caught.exception), "bruno_output_too_large")
        self.assertIsNotNone(process.poll())

    def test_timeout_stops_process(self):
        process = self.process("import time; time.sleep(30)")
        with self.assertRaises(FetchError) as caught:
            _capture(process, 0.05, 1000)
        self.assertEqual(str(caught.exception), "bruno_timeout")
        self.assertIsNotNone(process.poll())

    def test_cancellation_stops_process(self):
        process = self.process("import time; time.sleep(30)")
        with patch("scripts.boise_collect.bruno_client.selectors.DefaultSelector", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _capture(process, 5, 1000)
        self.assertIsNotNone(process.poll())

    def test_sigterm_stops_runner_and_retains_recoverable_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pid_file = root / "runner.pid"
            runner = root / "runner.py"
            runner.write_text(
                "import os, time\nfrom pathlib import Path\n"
                f"ready = Path({str(pid_file.with_suffix('.tmp'))!r})\n"
                "ready.write_text(str(os.getpid()))\n"
                f"ready.replace({str(pid_file)!r})\n"
                "time.sleep(30)\n"
            )
            script = textwrap.dedent("""\
                import json, signal, sys
                from pathlib import Path
                from unittest.mock import patch
                from scripts.boise_collect import __main__ as cli
                from scripts.boise_collect.bruno_client import BrunoClient

                def client(base, timeout):
                    instance = BrunoClient(base, timeout)
                    instance.node = sys.executable
                    instance.executable = Path(sys.argv[2])
                    return instance

                previous_handler = signal.getsignal(signal.SIGTERM)
                with patch.object(cli, 'BrunoClient', client):
                    code = cli.main(['scan', '--data-dir', sys.argv[1],
                        '--start', '2010-09-26', '--end', '2010-09-26',
                        '--base-url', 'http://127.0.0.1:1'])
                print(json.dumps({'handler_restored': signal.getsignal(signal.SIGTERM) == previous_handler}))
                raise SystemExit(code)
            """)
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(root / "archive"), str(runner)],
                cwd=Path(__file__).resolve().parents[3],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            runner_pid = None
            try:
                deadline = time.monotonic() + 10
                while not pid_file.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pid_file.exists(), "collector did not start its runner")
                runner_pid = int(pid_file.read_text())
                process.terminate()
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 130, stdout + stderr)
                self.assertIn("Interrupted; committed work is retained.", stderr)
                self.assertTrue(json.loads(stdout)["handler_restored"])
                with self.assertRaises(ProcessLookupError):
                    os.kill(runner_pid, 0)
                store = Store(root / "archive", "http://127.0.0.1:1")
                try:
                    store.recover_interrupted(time.time())
                    self.assertEqual(store.conn.execute("SELECT outcome FROM attempts").fetchone()[0], "interrupted")
                    self.assertEqual(store.next_resource(time.time(), set())["key"], "2010-09-26")
                finally:
                    store.close()
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
                if runner_pid is not None:
                    try:
                        os.killpg(runner_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_successful_capture_returns_exit_status(self):
        process = self.process("print('result'); raise SystemExit(7)")
        self.assertEqual(_capture(process, 5, 1000), (b"result\n", 7))


if __name__ == "__main__":
    unittest.main()
