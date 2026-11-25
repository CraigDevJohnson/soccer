"""S3 conditional writes and verified transfers for the hosted collector."""

from contextlib import closing
import base64
import hashlib
import json
from pathlib import Path
import shutil


class Conflict(Exception):
    """The object changed since the caller read it."""


def json_limit(key):
    # Journal entries contain allowlisted projections of an upstream response
    # limited to 8 MiB. Control and configuration contain only metadata.
    return 128 * 1024**2 if key.startswith("journal/") else 1024**2


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode()


class S3Objects:
    def __init__(self, client, bucket):
        self.client = client
        self.bucket = bucket

    def read(self, key):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with closing(response["Body"]) as body:
            limit = json_limit(key)
            if not 0 < response["ContentLength"] <= limit:
                raise ValueError("archive JSON object exceeds its size limit")
            data = body.read(response["ContentLength"] + 1)
            if len(data) != response["ContentLength"]:
                raise ValueError("archive JSON transfer length differs")
            return json.loads(data), response["ETag"]

    def mode(self):
        from botocore.exceptions import ClientError

        try:
            value, _ = self.read("settings/collection.json")
        except ClientError as error:
            if error.response["Error"]["Code"] in ("AccessDenied", "403", "NoSuchKey", "404"):
                return "off"
            raise
        if not isinstance(value, dict) or set(value) != {"mode"} or value["mode"] not in ("off", "manual", "daily"):
            raise ValueError("invalid archive collection setting")
        return value["mode"]

    def status(self, key):
        from botocore.exceptions import ClientError

        try:
            value, _ = self.read(key)
            return value
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code in ("NoSuchKey", "404"):
                return {"status": "idle"}
            # Without ListBucket, S3 also uses 403 for a missing key. Keep an
            # unreadable state distinguishable from a confirmed empty archive.
            if code in ("AccessDenied", "403"):
                return {"status": "checkpoint_missing_or_unreadable"}
            raise

    def _put(self, key, body, previous, **options):
        from botocore.exceptions import ClientError

        condition = {"IfNoneMatch": "*"} if previous is None else {"IfMatch": previous}
        try:
            return self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                                          **condition, **options)["ETag"]
        except ClientError as error:
            if error.response["Error"]["Code"] in (
                "PreconditionFailed", "ConditionalRequestConflict", "412", "409",
            ):
                raise Conflict from error
            raise

    def write(self, key, value, previous=None):
        data = encode(value)
        if len(data) > json_limit(key):
            raise ValueError("archive JSON object exceeds its size limit")
        return self._put(key, data, previous, ContentType="application/json")

    def download(self, key, path, limit, *, exact=False):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with closing(response["Body"]) as src, path.open("xb") as dst:
            length = response["ContentLength"]
            if not 0 < length <= limit or (exact and length != limit):
                raise ValueError("archive transfer length exceeds the manifest or local limit")
            for chunk in src.iter_chunks(chunk_size=1024 * 1024):
                if dst.tell() + len(chunk) > length:
                    raise ValueError("archive transfer exceeds declared length")
                dst.write(chunk)
            if dst.tell() != length:
                raise ValueError("incomplete archive transfer")

    def publish_file(self, key, path):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").digest()
            stream.seek(0)
            try:
                self._put(key, stream, None, ChecksumSHA256=base64.b64encode(digest).decode())
            except Conflict:
                pass
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with closing(response["Body"]) as body:
            if response["ContentLength"] != path.stat().st_size:
                raise ValueError("published archive readback length differs")
            received = hashlib.sha256()
            size = 0
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                size += len(chunk)
                if size > response["ContentLength"]:
                    raise ValueError("published archive readback exceeds declared length")
                received.update(chunk)
        if received.digest() != digest:
            raise ValueError("published archive readback differs")


class Generations:
    def __init__(self, objects, workdir, release):
        self.objects = objects
        self.workdir = Path(workdir)
        self.release = release

    def restore(self, generation):
        from .publication import MAX_MANIFEST_BYTES, load_manifest, restore_generation

        source = self.workdir / "source" / generation
        manifest = source / "manifest.json"
        if not source.exists():
            # Draining the next occurrence can switch generations within one
            # invocation. Only the current job needs a local working copy.
            for name in ("data", "source", "output"):
                old = self.workdir / name
                if old.exists():
                    shutil.rmtree(old)
            source.mkdir(parents=True)
            self.objects.download(f"generations/{generation}/manifest.json", manifest, MAX_MANIFEST_BYTES)
            doc = load_manifest(manifest)
            if doc["generation"] != generation:
                raise ValueError("source manifest differs from active generation")
            archive = doc["archive"]
            # Publication also needs a frozen SQLite backup and its evidence.
            if 2 * (archive["unpacked_bytes"] + archive["bytes"]) + 64 * 1024**2 > shutil.disk_usage(source).free:
                raise ValueError("generation exceeds worker scratch space")
            self.objects.download(f"generations/{generation}/archive.tar.gz", source / "archive.tar.gz",
                                  archive["bytes"], exact=True)
        dataset = self.workdir / "data"
        if dataset.exists():
            shutil.rmtree(dataset)
        restore_generation(manifest, dataset)
        return dataset

    def publish(self, dataset, completed_at):
        from .publication import export_generation
        from .report import write_report

        report = write_report(dataset, dataset / "reports", now=completed_at)
        if not report["evidence"]["complete"]:
            raise ValueError("source evidence is incomplete")
        del report
        manifest = export_generation(dataset, self.workdir / "output", self.release)
        generation = manifest.parent.name
        for name in ("archive.tar.gz", "manifest.json"):
            self.objects.publish_file(f"generations/{generation}/{name}", manifest.parent / name)
        return generation
