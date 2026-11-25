"""Stage an archive, activate its baseline, or rehearse recovery of the active generation."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .publication import MAX_ARCHIVE_BYTES, MAX_MANIFEST_BYTES, load_manifest, verify_generation


FILES = {"archive.tar.gz": MAX_ARCHIVE_BYTES, "manifest.json": MAX_MANIFEST_BYTES}


def digest(path: Path) -> bytes:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


class S3:
    """Use the configured AWS CLI v2 session. Each write creates one key."""

    def __init__(self, environment: str, account: str, *, recovery=False):
        if environment not in {"dev", "prod"} or not re.fullmatch(r"[0-9]{12}", account):
            raise ValueError("expected the workloads account and dev or prod")
        if recovery and environment != "prod":
            raise ValueError("the independent recovery bucket is production-only")
        prefix = "prod-recovery" if recovery else environment
        self.bucket = f"soccer-archive-{prefix}-{account}"
        self.account = account

    def call(self, operation, key, *arguments, allow_existing=False):
        result = subprocess.run(
            ["aws", "s3api", operation, "--bucket", self.bucket, "--key", key,
             "--region", "us-west-2", "--expected-bucket-owner", self.account,
             "--no-cli-pager", "--output", "json", *arguments],
            capture_output=True, check=False,
        )
        if result.returncode:
            if allow_existing and b"(PreconditionFailed)" in result.stderr:
                return None
            code = re.search(rb"\(([A-Za-z0-9_.]+)\)", result.stderr)
            category = code[1].decode() if code else "CLI error"
            raise RuntimeError(f"S3 {operation} failed ({category})")
        return json.loads(result.stdout or b"{}")

    def download(self, key, path, limit):
        head = self.call("head-object", key)
        if not 0 < head["ContentLength"] <= limit:
            raise ValueError("remote archive file is outside size limits")
        self.call("get-object", key, "--if-match", head["ETag"], str(path))
        if path.stat().st_size != head["ContentLength"]:
            raise ValueError("short archive download")

    def head_version(self, key, version=None):
        args = ("--version-id", version) if version else ()
        return self.call("head-object", key, *args)

    def download_version(self, key, path, limit, head):
        size = head["ContentLength"]
        if not 0 < size <= limit:
            raise ValueError("remote archive version is outside size limits")
        received = self.call("get-object", key, "--version-id", head["VersionId"],
                             "--if-match", head["ETag"], str(path))
        if (received.get("VersionId") != head["VersionId"] or received.get("ETag") != head["ETag"]
                or path.stat().st_size != size):
            raise ValueError("object readback differs from the selected version")

    def create(self, key, path):
        checksum = base64.b64encode(digest(path)).decode("ascii")
        result = self.call("put-object", key, "--body", str(path), "--if-none-match", "*",
                           "--checksum-sha256", checksum, allow_existing=True)
        return result is not None


def check_generation(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid generation ID")
    return value


def create_verified(store, key, path, temp_dir):
    # An interrupted PUT may already have succeeded. In either case verify the stored bytes.
    store.create(key, path)
    readback = temp_dir / "readback"
    store.download(key, readback, path.stat().st_size)
    if digest(readback) != digest(path):
        raise ValueError("existing or uploaded object differs from the verified generation")
    readback.unlink()


def stage(store, manifest):
    manifest = Path(manifest)
    with tempfile.TemporaryDirectory(prefix="soccer-stage-") as directory:
        temp = Path(directory)
        # Verify the exact private copy sent. The operator's export can move or change later.
        copy = temp / "generation"
        copy.mkdir()
        for name, limit in FILES.items():
            path = manifest if name == "manifest.json" else manifest.parent / name
            if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
                raise ValueError("invalid local generation file")
            shutil.copyfile(path, copy / name)
        verified = verify_generation(copy / "manifest.json")
        generation = check_generation(verified["generation"])
        for name in FILES:
            create_verified(store, f"staging/{generation}/{name}", copy / name, temp)
    return {"staged": generation}


def download_verified(store, prefix, generation, temp):
    manifest = temp / "manifest.json"
    store.download(f"{prefix}/{generation}/manifest.json", manifest, MAX_MANIFEST_BYTES)
    doc = load_manifest(manifest)
    if doc["generation"] != generation:
        raise ValueError("object prefix differs from generation identity")
    store.download(f"{prefix}/{generation}/archive.tar.gz", temp / "archive.tar.gz", doc["archive"]["bytes"])
    return doc, verify_generation(manifest)


def pointer_generation(pointer):
    value = json.loads(pointer.read_bytes())
    if not isinstance(value, dict) or set(value) != {"generation"}:
        raise ValueError("invalid active pointer")
    return check_generation(value["generation"])


def recovery_receipt(manifest, result):
    return {"generation": result["generation"], "archive_sha256": result["archive_sha256"],
            "evidence_files_verified": result["evidence_files_verified"],
            "table_rows": {table: info["rows"] for table, info in manifest["tables"].items()}}


def verify_active(store):
    """Restore the generation named by one conditional read of the active pointer."""
    with tempfile.TemporaryDirectory(prefix="soccer-recovery-") as directory:
        temp = Path(directory)
        pointer = temp / "active.json"
        store.download("control/active.json", pointer, 64 * 1024)
        generation = pointer_generation(pointer)
        manifest, result = download_verified(store, "generations", generation, temp)
        return recovery_receipt(manifest, result)


class ReplicationPending(RuntimeError):
    """The chosen source version is still queued for S3 replication."""


def replicated_file(primary, recovery, key, path, limit, sleep):
    source = primary.head_version(key)
    version = source.get("VersionId")
    if not isinstance(version, str) or not version or version == "null":
        raise ValueError("source object lacks a version ID")
    for delay in (2, 5, 10, 20):
        if source.get("ReplicationStatus") != "PENDING":
            break
        sleep(delay)
        source = primary.head_version(key, version)
        if source.get("VersionId") != version:
            raise ValueError("source version changed during replication readback")
    if source.get("ReplicationStatus") == "PENDING":
        raise ReplicationPending(f"replication is PENDING for {key} version {version}; retry verification")
    if source.get("ReplicationStatus") not in {"COMPLETE", "COMPLETED"}:
        raise ValueError(f"source replication status is not complete for {key}")
    # S3 live replication preserves source version IDs. Read that specific version
    # from both buckets, even if another job has already advanced the pointer.
    replica = recovery.head_version(key, version)
    if replica.get("VersionId") != version or replica.get("ReplicationStatus") != "REPLICA":
        raise ValueError(f"destination version or replication status differs for {key}")
    if replica["ContentLength"] != source["ContentLength"]:
        raise ValueError("replica and source version sizes differ")
    original = path.parent / "source-readback"
    primary.download_version(key, original, limit, source)
    recovery.download_version(key, path, limit, replica)
    if digest(original) != digest(path):
        raise ValueError("replica and source version checksums differ")
    original.unlink()
    return version


def verify_recovery(primary, recovery, sleep=time.sleep):
    with tempfile.TemporaryDirectory(prefix="soccer-replica-") as directory:
        temp = Path(directory)
        pointer = temp / "active.json"
        version = replicated_file(primary, recovery, "control/active.json", pointer, 64 * 1024, sleep)
        generation = pointer_generation(pointer)
        prefix = f"generations/{generation}"
        manifest = temp / "manifest.json"
        replicated_file(primary, recovery, f"{prefix}/manifest.json", manifest, MAX_MANIFEST_BYTES, sleep)
        doc = load_manifest(manifest)
        if doc["generation"] != generation:
            raise ValueError("replicated manifest differs from the active pointer")
        replicated_file(primary, recovery, f"{prefix}/archive.tar.gz", temp / "archive.tar.gz",
                        doc["archive"]["bytes"], sleep)
        result = verify_generation(manifest)
        return {"status": "verified", "source_pointer_version": version, **recovery_receipt(doc, result)}


def promote(store, generation):
    generation = check_generation(generation)
    with tempfile.TemporaryDirectory(prefix="soccer-import-") as directory:
        temp = Path(directory)
        download_verified(store, "staging", generation, temp)
        for name in FILES:
            create_verified(store, f"generations/{generation}/{name}", temp / name, temp)
        # Only an empty destination can be activated by the import role.
        pointer = temp / "active.json"
        pointer.write_text(json.dumps({"generation": generation}, separators=(",", ":")))
        store.create("control/active.json", pointer)
        store.download("control/active.json", temp / "active-readback.json", 64 * 1024)
        active = json.loads((temp / "active-readback.json").read_bytes())
        if active != {"generation": generation}:
            raise ValueError("the archive has a different active checkpoint; import stopped")
    return {"active": generation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="operation", required=True)
    for name in ("stage", "promote", "verify-active", "verify-recovery"):
        sub = subs.add_parser(name)
        sub.add_argument("--environment", choices=["dev", "prod"], required=True)
        sub.add_argument("--account", required=True)
        if name == "stage":
            sub.add_argument("--manifest", type=Path, required=True)
        elif name == "promote":
            sub.add_argument("--generation", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    store = S3(args.environment, args.account)
    if args.operation == "stage":
        result = stage(store, args.manifest)
    elif args.operation == "promote":
        result = promote(store, args.generation)
    elif args.operation == "verify-active":
        result = verify_active(store)
    else:
        result = verify_recovery(store, S3(args.environment, args.account, recovery=True))
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except ReplicationPending as error:
        print(json.dumps({"status": "replication_pending", "detail": str(error)}))
        print(str(error), file=sys.stderr)
        sys.exit(75)
    except (OSError, KeyError, ValueError, RuntimeError) as error:
        sys.exit(str(error))
