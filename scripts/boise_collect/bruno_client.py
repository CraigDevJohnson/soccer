"""Execute the pinned, public-only Bruno collection without retaining raw reports."""

from __future__ import annotations

import base64
import binascii
from datetime import date
import json
import math
import os
from pathlib import Path
import secrets
import selectors
import shutil
import signal
import subprocess
import tempfile
import time

from .collector import MAX_BODY, FetchError, Response, request_path, validated_origin

BRUNO_VERSION = "4.4.0"
COLLECTION = Path(__file__).resolve().parent / "bruno"
FRAME = b"BOISE_BRUNO_RESULT "
COLLECTION_FILES = ("bruno.json", "collection.bru", "day.bru", "division.bru", "catalog.bru")


def _stop_process(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _capture(process: subprocess.Popen, timeout: float, limit: int) -> tuple[bytes, int]:
    """Bound both IPC memory and the lifetime of a stopped or broken runner."""
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FetchError("bruno_timeout")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(output) + len(chunk) > limit:
                        raise FetchError("bruno_output_too_large")
                    output.extend(chunk)
        try:
            code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise FetchError("bruno_timeout") from None
        return bytes(output), code
    finally:
        _stop_process(process)
        process.stdout.close()


def _decode(output: bytes, code: int, request_id: str) -> Response:
    frames = [line[len(FRAME):] for line in output.splitlines() if line.startswith(FRAME)]
    if len(frames) != 1:
        raise FetchError("bruno_process_failed" if code else "bruno_invalid_result")
    try:
        result = json.loads(frames[0])
        if (not isinstance(result, dict) or type(result.get("version")) is not int
                or result.get("version") != 1 or result.get("requestId") != request_id):
            raise ValueError
        status, retry_after = result.get("status"), result.get("retryAfter")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise ValueError
        if retry_after is not None and (not isinstance(retry_after, str) or len(retry_after) > 8192):
            raise ValueError
        if "error" in result:
            if set(result) != {"version", "requestId", "error", "status", "retryAfter"}:
                raise ValueError
            if result["error"] not in ("body_too_large", "network_error"):
                raise ValueError
            raise FetchError(result["error"], status, retry_after)
        if code:
            raise FetchError("bruno_process_failed", status, retry_after)
        if set(result) != {"version", "requestId", "status", "retryAfter", "bodyBase64"} or status is None:
            raise ValueError
        if not isinstance(result["bodyBase64"], str):
            raise ValueError
        body = base64.b64decode(result["bodyBase64"], validate=True)
        if len(body) > MAX_BODY:
            raise FetchError("body_too_large", status, retry_after)
        return Response(status, body, retry_after)
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        raise FetchError("bruno_invalid_result") from None


class BrunoClient:
    def __init__(self, base_url: str, timeout: float = 15):
        self.base_url = validated_origin(base_url)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.timeout = timeout
        node = shutil.which("node")
        package = COLLECTION / "node_modules" / "@usebruno" / "cli"
        self.executable = package / "bin" / "bru.js"
        try:
            version = json.loads((package / "package.json").read_text())["version"]
        except (OSError, ValueError, KeyError, TypeError):
            version = None
        if not node or version != BRUNO_VERSION or not self.executable.is_file():
            raise ValueError("Bruno 4.4.0 and Node are required; run task collector:setup")
        self.node = node
        self.sources = {name: (COLLECTION / name).read_bytes() for name in COLLECTION_FILES}

    def get(self, kind: str, key: str) -> Response:
        resource_path = request_path(kind, key)
        request_id = secrets.token_hex(16)
        variables = {
            "baseUrl": self.base_url,
            "resourcePath": resource_path,
            "requestId": request_id,
            "maxBody": str(MAX_BODY),
            "timeoutMs": str(max(1, math.ceil(self.timeout * 1000))),
        }
        if kind == "day":
            variables["date"] = date.fromisoformat(key).strftime("%d-%m-%Y")
        elif kind == "division":
            variables["divisionId"] = key
        with tempfile.TemporaryDirectory(prefix="boise-bruno-") as folder:
            for name, content in self.sources.items():
                (Path(folder) / name).write_bytes(content)
            command = [self.node, str(self.executable), "run", kind + ".bru",
                       "--sandbox", "developer", "--disable-cookies", "--noproxy"]
            for name, value in variables.items():
                command.extend(("--env-var", name + "=" + value))
            process = None
            try:
                process = subprocess.Popen(
                    command, cwd=folder, env={"NO_COLOR": "1", "LANG": "C.UTF-8"},
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                output, code = _capture(process, self.timeout + 15, ((MAX_BODY + 2) // 3) * 4 + 65536)
            except OSError:
                raise FetchError("bruno_process_failed") from None
            finally:
                # Also cover interruption between process creation and capture.
                if process is not None:
                    _stop_process(process)
                    process.stdout.close()
        return _decode(output, code, request_id)
