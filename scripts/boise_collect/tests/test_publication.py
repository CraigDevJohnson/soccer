"""Exercise private archive generations through their offline export/restore boundary."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import contextlib
import fcntl
import gzip
import hashlib
import json
import sqlite3
import tarfile
import unittest

from scripts.boise_collect.collector import ORIGIN
from scripts.boise_collect.model import parse_response
from scripts.boise_collect.publication import export_generation, restore_generation, verify_generation
from scripts.boise_collect.report import write_report
from scripts.boise_collect.store import Store


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.output = self.root / "generations"
        self.release = "a" * 40
        self.day = "2026-11-11"
        store = Store(self.source, ORIGIN)
        try:
            store.queue_interval(date.fromisoformat(self.day), date.fromisoformat(self.day))
            attempt = store.start_attempt("day", self.day, 1)
            parsed = parse_response("day", self.day, {"data": {}})
            store.succeed(attempt, parsed, "a" * 64, 2)
        finally:
            store.close()
        (self.source / "collector.lock").touch()
        write_report(self.source, self.source / "reports", now=3)

    def export(self):
        return export_generation(self.source, self.output, self.release)

    def test_restore_preserves_rows_and_only_transfers_archive_files(self):
        (self.source / "local-environment").write_text("private")
        (self.source / "evidence" / ("b" * 64 + ".json")).write_text("orphan")
        manifest = self.export()
        receipt = verify_generation(manifest)
        target = self.root / "restored"
        self.assertEqual(restore_generation(manifest, target), receipt)
        self.assertEqual(self.export(), manifest)
        with contextlib.closing(sqlite3.connect(target / "collector.sqlite3")) as db:
            self.assertEqual(db.execute("select kind,key,status from resources").fetchall(),
                             [("day", self.day, "success")])
            file, digest = db.execute("select evidence_path,evidence_sha256 from snapshots").fetchone()
        self.assertEqual(hashlib.sha256((target / file).read_bytes()).hexdigest(), digest)
        self.assertEqual((target / "reports/coverage.json").read_bytes(),
                         (self.source / "reports/coverage.json").read_bytes())
        self.assertEqual(list((target / "evidence").iterdir()), [target / file])
        self.assertFalse((target / "local-environment").exists())
        self.assertEqual(target.stat().st_mode & 0o077, 0)
        self.assertEqual((target / file).stat().st_mode & 0o077, 0)
        with self.assertRaises(ValueError):
            restore_generation(manifest, target)

    def test_busy_writer(self):
        with (self.source / "collector.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError, "writer"):
                self.export()

    def test_missing_altered_or_symlinked_evidence_fails_without_generation(self):
        evidence, = (self.source / "evidence").iterdir()
        outside = self.root / "outside"
        evidence.rename(outside)
        for mode in ("missing", "symlink", "corrupt"):
            with self.subTest(mode=mode):
                if mode == "symlink":
                    evidence.symlink_to(outside)
                elif mode == "corrupt":
                    evidence.unlink()
                    evidence.write_bytes(b"{}")
                with self.assertRaises((ValueError, OSError)):
                    self.export()
                self.assertEqual(list(self.output.iterdir()), [])

    def test_wrong_facility_and_unknown_schema(self):
        with contextlib.closing(sqlite3.connect(self.source / "collector.sqlite3")) as db:
            db.execute("insert into teams(id,facility_id,snapshot_id,data_json) values('1',7,1,'{}')")
            db.commit()
            with self.assertRaisesRegex(ValueError, "facility"):
                self.export()
            db.execute("delete from teams")
            db.execute("update meta set value='bad' where key='schema_version'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "schema"):
            self.export()

    def test_restore_rejects_unlisted_members_links_and_contents_after_end(self):
        manifest = self.export()
        bundle = manifest.parent / "archive.tar.gz"
        original = gzip.decompress(bundle.read_bytes())
        baseline = json.loads(manifest.read_bytes())
        replacement = tarfile.TarInfo("unlisted.json")
        replacement.mode = 0o600
        replacement.size = tarfile.TarInfo.frombuf(original[:512], "ascii", "strict").size
        link = tarfile.TarInfo("collector.sqlite3")
        link.type = tarfile.SYMTYPE
        link.linkname = "other"
        bad_layouts = (replacement.tobuf() + original[512:], link.tobuf() + original[512:],
                       original + b"extra")
        for payload in bad_layouts:
            with self.subTest(header=payload[:16]):
                packed = gzip.compress(payload, mtime=0)
                bundle.write_bytes(packed)
                doc = baseline.copy()
                del doc["generation"]
                doc["archive"] = dict(doc["archive"], sha256=hashlib.sha256(packed).hexdigest(),
                                      bytes=len(packed))
                canonical = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
                doc["generation"] = hashlib.sha256(canonical).hexdigest()
                manifest.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n")
                dest = self.root / "restored"
                with self.assertRaises(ValueError):
                    restore_generation(manifest, dest)
                self.assertFalse(dest.exists())

    def test_matching_private_file_hash_does_not_bypass_evidence_sanitization(self):
        path, = (self.source / "evidence").iterdir()
        data = json.dumps({"data": {}, "account_metadata": "private"}).encode()
        sha = hashlib.sha256(data).hexdigest()
        name = f"evidence/{sha}.json"
        (self.source / name).write_bytes(data)
        path.unlink()
        with contextlib.closing(sqlite3.connect(self.source / "collector.sqlite3")) as db:
            db.execute("update snapshots set evidence_path=?, evidence_sha256=?", (name, sha))
            db.commit()
        with self.assertRaisesRegex(ValueError, "retention"):
            self.export()

    def test_tampered_archive_blocks_restore_and_repeat_publication(self):
        manifest = self.export()
        with (manifest.parent / "archive.tar.gz").open("ab") as file:
            file.write(b"damage")
        dest = self.root / "new"
        with self.assertRaises(ValueError):
            restore_generation(manifest, dest)
        self.assertFalse(dest.exists())
        with self.assertRaises(ValueError):
            self.export()

    def test_report_is_rebuilt_against_failure_ledger_before_export(self):
        store = Store(self.source, ORIGIN)
        try:
            store.requeue("day", self.day)
            attempt = store.start_attempt("day", self.day, 4)
            store.fail(attempt, "failed", 500, "upstream_status", None, None, 5)
        finally:
            store.close()
        with self.assertRaisesRegex(ValueError, "report"):
            self.export()
        write_report(self.source, self.source / "reports", now=6)
        self.assertEqual(verify_generation(self.export())["tables_verified"], 17)

    def test_extra_report_fields_and_false_completeness_are_rejected(self):
        path = self.source / "reports/coverage.json"
        for change in ({"all_time_complete": True}, {"account_metadata": "private"}):
            with self.subTest(change=list(change)):
                write_report(self.source, self.source / "reports", now=3)
                report = json.loads(path.read_bytes())
                report.update(change)
                path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
                with self.assertRaisesRegex(ValueError, "report"):
                    self.export()

    def test_unsupported_release_or_unsafe_output_is_rejected(self):
        with self.assertRaises(ValueError):
            export_generation(self.source, self.output, "latest")
        with self.assertRaises(ValueError):
            export_generation(self.source, self.source / "export", self.release)
        self.assertFalse((self.source / "export").exists())
