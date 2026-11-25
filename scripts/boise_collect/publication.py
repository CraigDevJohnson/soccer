"""Export, verify and restore a private, portable Boise archive generation offline."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
from tempfile import TemporaryDirectory

from .collector import ORIGIN
from .model import parse_response
from .report import _markdown, build_report
from .store import SCHEMA_VERSION, _REFRESH_SCHEMA, _SCHEMA

ARCHIVE = "archive.tar.gz"
DATABASE = "collector.sqlite3"
REPORTS = ("reports/coverage.json", "reports/coverage.md")
INDEX = "evidence.sha256"
CORE = (DATABASE, *REPORTS, INDEX)
MAX_MANIFEST_BYTES = 1024**2
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_CONTENT_BYTES = 4 * 1024**3
MAX_ENTRIES = 250_000
HEX = re.compile(r"[0-9a-f]{64}")
RELEASE = re.compile(r"[0-9a-f]{40}")
EVIDENCE = re.compile(r"evidence/([0-9a-f]{64})\.json")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _manifest_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _regular(path: Path) -> Path:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"expected a regular file: {path.name}")
    return path


def _directory(path: Path) -> Path:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError(f"expected a directory: {path.name}")
    return path


def _hash_file(path: Path) -> dict:
    with _regular(path).open("rb") as handle:
        return {"bytes": os.fstat(handle.fileno()).st_size,
                "sha256": hashlib.file_digest(handle, "sha256").hexdigest()}


def _open_db(root: Path):
    path = _regular(root / DATABASE)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA query_only=ON")
    return db


def _schema(db):
    return set(db.execute("SELECT type,name,sql FROM sqlite_schema"))


def _database_summary(db) -> dict:
    schema = _schema(db)
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.executescript(_SCHEMA)
        original = _schema(expected)
        expected.executescript(_REFRESH_SCHEMA)
        if schema not in (original, _schema(expected)):
            raise ValueError("unsupported collector database schema")
    meta = dict(db.execute("SELECT key,value FROM meta"))
    if (set(meta) != {"base_url", "next_request_at", "schema_version"}
            or meta["schema_version"] != SCHEMA_VERSION or meta["base_url"] != ORIGIN):
        raise ValueError("unexpected collector schema version or source origin")
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("collector database failed integrity or foreign-key checks")
    for table in ("games", "teams", "divisions"):
        if db.execute(f"SELECT 1 FROM {table} WHERE facility_id IS NOT 4 LIMIT 1").fetchone():
            raise ValueError("collector database contains another facility")
    # Include rowid for tables without a primary key; duplicate observations and
    # all declared identity columns must survive export and restore unchanged.
    tables = {}
    for table in sorted(name for kind, name, _ in schema if kind == "table"):
        digest = hashlib.sha256()
        count = 0
        for row in db.execute(f'SELECT rowid,* FROM "{table}" ORDER BY rowid'):
            digest.update(canonical(row) + b"\n")
            count += 1
        tables[table] = {"rows": count, "sha256": digest.hexdigest()}
    updates = ["SELECT max(coalesce(finished_at,started_at)) FROM attempts"]
    if "refresh_jobs" in tables:
        updates.extend(["SELECT max(coalesce(completed_at,created_at)) FROM refresh_jobs",
                        "SELECT max(recorded_at) FROM refresh_gap_acknowledgments"])
    latest = db.execute("SELECT max(timestamp) FROM (SELECT NULL AS timestamp UNION ALL "
                        + " UNION ALL ".join(updates) + ")").fetchone()[0]
    snapshot_as_of = None if latest is None else datetime.fromtimestamp(latest, timezone.utc).isoformat()
    return {
        "schema_version": meta["schema_version"], "source_origin": ORIGIN, "facility_id": 4,
        "snapshot_as_of": snapshot_as_of, "tables": tables,
        "intervals": [list(r) for r in db.execute("SELECT start_day,end_day FROM intervals ORDER BY start_day,end_day")],
        "resource_statuses": [list(r) for r in db.execute(
            "SELECT kind,status,count(*) FROM resources GROUP BY kind,status ORDER BY kind,status")],
        "observed_game_dates": list(db.execute("SELECT min(substr(start_local,1,10)),max(substr(start_local,1,10)) FROM games").fetchone()),
    }


def _references(db) -> list:
    refs = db.execute("SELECT DISTINCT evidence_path,evidence_sha256,kind,key FROM snapshots ORDER BY 1,3,4").fetchall()
    for name, digest, _, _ in refs:
        if not isinstance(digest, str) or not HEX.fullmatch(digest) or name != f"evidence/{digest}.json":
            raise ValueError("invalid snapshot evidence reference")
    return refs


def _new_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _copy(source, target):
    with _regular(source).open("rb") as src, _new_file(target) as dst:
        shutil.copyfileobj(src, dst)


def _freeze(source, root):
    for name in ("evidence", "reports"):
        _directory(source / name)
        (root / name).mkdir(mode=0o700)
    with _regular(source / "collector.lock").open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("collector writer holds the dataset lock") from None
        with closing(_open_db(source)) as original:
            original.execute("BEGIN")
            summary = _database_summary(original)
            refs = _references(original)
            with _new_file(root / DATABASE):
                pass
            with closing(sqlite3.connect(root / DATABASE)) as backup:
                original.backup(backup)
        for name in sorted({ref[0] for ref in refs}):
            _copy(source / name, root / name)
        for name in REPORTS:
            _copy(source / name, root / name)
    return summary


def _verified_report(root, expected_json_hash):
    # The report writer publishes these files separately after releasing the
    # collection lock. Rebuild both from the frozen ledger at their saved time.
    md = _regular(root / REPORTS[1])
    with md.open("rb") as handle:
        header = handle.read(1024)
    match = re.search(rb"^Generated ([0-9T:.+Z-]+)\. Facility 4\.", header, re.MULTILINE)
    if not match:
        raise ValueError("coverage report lacks its generation time")
    timestamp = datetime.fromisoformat(match[1].decode().replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("coverage report lacks its timezone")
    report = build_report(root, now=timestamp.timestamp())
    digest = hashlib.sha256()
    for piece in json.JSONEncoder(indent=2, sort_keys=True).iterencode(report):
        digest.update(piece.encode())
    digest.update(b"\n")
    if (digest.hexdigest() != expected_json_hash
            or _hash_file(md)["sha256"] != hashlib.sha256(_markdown(report).encode()).hexdigest()):
        raise ValueError("coverage reports disagree with the frozen archive")
    return report


def _inventory(root, *, write_index=False):
    with closing(_open_db(root)) as db:
        summary = _database_summary(db)
        refs = _references(db)
    index_lines = []
    evidence_bytes = 0
    last_name = None
    for name, digest, kind, key in refs:
        if name != last_name:
            file = _regular(root / name)
            # The wire body limit is 8 MiB; JSON escaping can expand one byte to six.
            if file.stat().st_size > 64 * 1024**2:
                raise ValueError("snapshot evidence exceeds collector limits")
            data = file.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("snapshot evidence checksum mismatch")
            document = json.loads(data)
            evidence_bytes += len(data)
            index_lines.append(f"{digest}  {name}\n")
            last_name = name
        # The collector stores the catalog's rows in a facility-stamped wrapper.
        wire = document.get("upcoming_leagues") if kind == "catalog" else document
        if parse_response(kind, key, wire).evidence != document:
            raise ValueError("snapshot evidence contains fields outside the collector's retention contract")
    if len(index_lines) != len(list((root / "evidence").iterdir())):
        raise ValueError("archive contains unreferenced evidence")
    index = "".join(index_lines).encode()
    if write_index:
        with _new_file(root / INDEX) as handle:
            handle.write(index)
    elif _regular(root / INDEX).read_bytes() != index:
        raise ValueError("evidence index differs from snapshot references")

    summary["files"] = {name: _hash_file(root / name) for name in CORE}
    report = _verified_report(root, summary["files"][REPORTS[0]]["sha256"])
    summary["coverage"] = {key: report["coverage"][key] for key in (
        "start_day", "end_day", "requested_dates", "dates_with_verified_evidence", "all_dates_checked",
        "all_dates_evidenced", "all_time_complete", "retention_boundary")}
    summary["interval_complete"] = report["inventory_complete_for_declared_interval"]
    summary["evidence_count"] = len(index_lines)
    summary["evidence_bytes"] = evidence_bytes
    return summary


def _pack(root, archive):
    names = [*CORE, *sorted(p.relative_to(root).as_posix() for p in (root / "evidence").iterdir())]
    size = 0
    with _new_file(archive) as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
            for name in names:
                with _regular(root / name).open("rb") as src:
                    info = tarfile.TarInfo(name)
                    info.mode = 0o600
                    info.size = os.fstat(src.fileno()).st_size
                    size += info.size
                    tar.addfile(info, src)
    details = dict(_hash_file(archive), unpacked_bytes=size, files=len(names))
    if details["bytes"] > MAX_ARCHIVE_BYTES or size > MAX_CONTENT_BYTES or len(names) > MAX_ENTRIES:
        raise ValueError("archive exceeds transfer limits")
    return details


@contextmanager
def _publish_lock(parent):
    fd = os.open(parent, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def export_generation(source_dir: Path, output_dir: Path, release: str) -> Path:
    """Freeze one dataset and atomically publish an immutable local generation."""
    if not RELEASE.fullmatch(release):
        raise ValueError("collector release must be a full lowercase Git commit SHA")
    source = _directory(Path(source_dir)).resolve()
    output = Path(output_dir).resolve()
    if output.is_relative_to(source):
        raise ValueError("generation output must be outside the source archive")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with TemporaryDirectory(prefix=".export-", dir=output) as tmp:
        stage = Path(tmp)
        frozen = stage / "frozen"
        frozen.mkdir(mode=0o700)
        source_summary = _freeze(source, frozen)
        inventory = _inventory(frozen, write_index=True)
        if any(inventory[k] != v for k, v in source_summary.items()):
            raise ValueError("SQLite backup differs from the locked source")
        bundle = stage / "generation"
        bundle.mkdir(mode=0o700)
        manifest = dict(inventory, format_version=1, collector_release=release, archive=_pack(frozen, bundle / ARCHIVE))
        generation = hashlib.sha256(canonical(manifest)).hexdigest()
        manifest["generation"] = generation
        encoded = _manifest_bytes(manifest)
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds transfer limit")
        with _new_file(bundle / "manifest.json") as handle:
            handle.write(encoded)
        dest = output / generation
        with _publish_lock(output):
            if os.path.lexists(dest):
                _directory(dest)
                if (_regular(dest / "manifest.json").read_bytes() != encoded
                        or _hash_file(dest / ARCHIVE) != {k: manifest["archive"][k] for k in ("bytes", "sha256")}):
                    raise ValueError("existing immutable generation differs from export")
            else:
                bundle.rename(dest)
        return dest / "manifest.json"


def load_manifest(manifest_path: Path) -> dict:
    """Check manifest identity and size limits before fetching its archive."""
    path = _regular(Path(manifest_path))
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds transfer limit")
    data = path.read_bytes()
    doc = json.loads(data)
    if not isinstance(doc, dict) or data != _manifest_bytes(doc):
        raise ValueError("expected a canonical archive manifest")
    manifest = doc.copy()
    generation = manifest.pop("generation", None)
    if generation != hashlib.sha256(canonical(manifest)).hexdigest():
        raise ValueError("generation ID does not match the manifest")
    if (type(doc.get("format_version")) is not int or doc["format_version"] != 1
            or not isinstance(doc.get("collector_release"), str) or not RELEASE.fullmatch(doc["collector_release"])):
        raise ValueError("invalid archive format or collector release")
    archive = doc.get("archive")
    if not isinstance(archive, dict) or set(archive) != {"bytes", "unpacked_bytes", "files", "sha256"}:
        raise ValueError("invalid archive descriptor")
    for field, limit in (("bytes", MAX_ARCHIVE_BYTES), ("unpacked_bytes", MAX_CONTENT_BYTES), ("files", MAX_ENTRIES)):
        if type(archive[field]) is not int or not 0 < archive[field] <= limit:
            raise ValueError("archive exceeds transfer limits")
    if not isinstance(archive["sha256"], str) or not HEX.fullmatch(archive["sha256"]):
        raise ValueError("invalid archive checksum")
    return doc


def _unpack(manifest_path, root):
    doc = load_manifest(manifest_path)
    archive = doc["archive"]
    path = Path(manifest_path).parent / ARCHIVE
    if _regular(path).stat().st_size != archive["bytes"] or _hash_file(path)["sha256"] != archive["sha256"]:
        raise ValueError("archive checksum or size mismatch")
    for name in ("reports", "evidence"):
        (root / name).mkdir(mode=0o700)
    seen = set()
    size = 0
    # Gzip the fixed USTAR layout produced above. Inspect each 512-byte header
    # before reading data so extended headers cannot allocate unbounded memory.
    with gzip.open(path, "rb") as stream:
        for _ in range(archive["files"]):
            header = stream.read(tarfile.BLOCKSIZE)
            try:
                info = tarfile.TarInfo.frombuf(header, "ascii", "strict")
            except (tarfile.HeaderError, UnicodeError):
                raise ValueError("invalid archive member header") from None
            name = info.name
            if not info.isreg() or name in seen or (name not in CORE and not EVIDENCE.fullmatch(name)):
                raise ValueError("unexpected, duplicate, or non-regular archive member")
            canonical_header = tarfile.TarInfo(name)
            canonical_header.mode, canonical_header.size = 0o600, info.size
            if header != canonical_header.tobuf(format=tarfile.USTAR_FORMAT):
                raise ValueError("unsupported archive member metadata")
            seen.add(name)
            size += info.size
            if info.size < 0 or size > archive["unpacked_bytes"]:
                raise ValueError("archive contents exceed the manifest")
            with _new_file(root / name) as dst:
                remaining = info.size
                while remaining:
                    chunk = stream.read(min(remaining, 1024**2))
                    if not chunk:
                        raise ValueError("truncated archive member")
                    dst.write(chunk)
                    remaining -= len(chunk)
            padding = -info.size % tarfile.BLOCKSIZE
            if stream.read(padding) != bytes(padding):
                raise ValueError("invalid archive member padding")
        end = 2 * tarfile.BLOCKSIZE
        end += -(stream.tell() + end) % tarfile.RECORDSIZE
        if stream.read(end) != bytes(end) or stream.read(1):
            raise ValueError("unexpected data after the last archive member")
    if not set(CORE).issubset(seen) or size != archive["unpacked_bytes"] or len(seen) != archive["files"]:
        raise ValueError("incomplete archive contents")
    observed = _inventory(root)
    expected = {k: v for k, v in doc.items() if k not in {"format_version", "generation", "collector_release", "archive"}}
    if canonical(observed) != canonical(expected):
        raise ValueError("restored archive inventory differs from manifest")
    return {"generation": doc["generation"], "archive_sha256": archive["sha256"],
            "archive_bytes": archive["bytes"], "evidence_files_verified": observed["evidence_count"],
            "tables_verified": len(observed["tables"]), "sqlite_integrity": "ok", "foreign_key_violations": 0}


def verify_generation(manifest_path: Path) -> dict:
    """Rehearse a complete restore in a new private temporary directory."""
    with TemporaryDirectory(prefix="boise-verify-") as tmp:
        return _unpack(manifest_path, Path(tmp))


def restore_generation(manifest_path: Path, output_dir: Path) -> dict:
    """Publish a verified, writable restore into a previously absent directory."""
    output = Path(output_dir).absolute()
    if os.path.lexists(output):
        raise ValueError("restore target already exists")
    with TemporaryDirectory(dir=output.parent, prefix=".restore-") as tmp:
        root = Path(tmp) / "restore"
        root.mkdir(mode=0o700)
        result = _unpack(manifest_path, root)
        (root / INDEX).unlink()
        with _new_file(root / "collector.lock"):
            pass
        with _publish_lock(output.parent):
            if os.path.lexists(output):
                raise ValueError("restore target already exists")
            root.rename(output)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--data-dir", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--release", required=True)
    for command in ("restore", "verify"):
        p = commands.add_parser(command)
        p.add_argument("--manifest", required=True, type=Path)
        if command == "restore":
            p.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            print(export_generation(args.data_dir, args.output, args.release))
        else:
            result = (restore_generation(args.manifest, args.output) if args.command == "restore"
                      else verify_generation(args.manifest))
            print(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, EOFError, KeyError, TypeError, sqlite3.Error, tarfile.TarError) as exc:
        print(f"archive {args.command} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
