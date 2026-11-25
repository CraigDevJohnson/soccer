"""Exercise first import with the same portable files used by the AWS CLI path."""

from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.boise_collect.collector import ORIGIN
from scripts.boise_collect.publication import export_generation
from scripts.boise_collect.report import write_report
from scripts.boise_collect.store import Store
from scripts.boise_collect.transfer import ReplicationPending, S3, promote, stage, verify_active, verify_recovery


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def create(self, key, path):
        if key in self.objects:
            return False
        self.objects[key] = path.read_bytes()
        return True

    def download(self, key, path, limit):
        data = self.objects[key]
        if len(data) > limit:
            raise ValueError("too large")
        path.write_bytes(data)


class VersionedStore:
    def __init__(self, files, status):
        self.versions = {key: {"v1": body} for key, body in files.items()}
        self.latest = {key: "v1" for key in files}
        self.status = status
        self.reads = []

    def head_version(self, key, version=None):
        version = version or self.latest[key]
        body = self.versions[key][version]
        return {"VersionId": version, "ReplicationStatus": self.status,
                "ETag": f'"{len(body)}"', "ContentLength": len(body)}

    def download_version(self, key, path, limit, head):
        version = head["VersionId"]
        body = self.versions[key][version]
        if not 0 < len(body) <= limit:
            raise ValueError("invalid version size")
        self.reads.append((key, version))
        path.write_bytes(body)


class TransferTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        source = self.root / "source"
        archive = Store(source, ORIGIN)
        try:
            archive.queue_interval(date(2026, 11, 1), date(2026, 11, 1))
        finally:
            archive.close()
        (source / "collector.lock").touch()
        write_report(source, source / "reports", now=1)
        self.manifest = export_generation(source, self.root / "generations", "a" * 40)
        self.generation = self.manifest.parent.name
        self.store = MemoryStore()

    def test_repeated_transfers_match_verified_source_and_keep_first_pointer(self):
        for _ in range(2):
            self.assertEqual(stage(self.store, self.manifest), {"staged": self.generation})
        self.assertNotIn("control/active.json", self.store.objects)
        for _ in range(2):
            self.assertEqual(promote(self.store, self.generation), {"active": self.generation})
        self.assertEqual(len(self.store.objects), 5)
        for name in ("manifest.json", "archive.tar.gz"):
            self.assertEqual(self.store.objects[f"generations/{self.generation}/{name}"],
                             (self.manifest.parent / name).read_bytes())

    def test_verify_active_restores_the_captured_generation_without_writes(self):
        stage(self.store, self.manifest)
        promote(self.store, self.generation)
        captured = []
        download = self.store.download

        def read(key, path, limit):
            download(key, path, limit)
            captured.append(key)
            if key == "control/active.json":
                # A worker promotes a later generation while verification is running.
                self.store.objects[key] = json.dumps({"generation": "b" * 64}).encode()

        self.store.download = read
        self.store.create = lambda *_: self.fail("verify must only read")
        receipt = verify_active(self.store)
        self.assertEqual(receipt["generation"], self.generation)
        self.assertEqual(receipt["table_rows"]["intervals"], 1)
        self.assertEqual(receipt["evidence_files_verified"], 0)
        self.assertEqual(receipt["archive_sha256"], json.loads(self.manifest.read_bytes())["archive"]["sha256"])
        self.assertEqual(captured, ["control/active.json", f"generations/{self.generation}/manifest.json",
                                    f"generations/{self.generation}/archive.tar.gz"])

    def test_verify_active_rejects_an_invalid_pointer_or_corrupt_published_generation(self):
        stage(self.store, self.manifest)
        promote(self.store, self.generation)
        self.store.objects["control/active.json"] = b'{"generation":"unknown"}'
        with self.assertRaises(ValueError):
            verify_active(self.store)
        self.store.objects["control/active.json"] = json.dumps({"generation": self.generation}).encode()
        self.store.objects[f"generations/{self.generation}/archive.tar.gz"] = b"damaged"
        with self.assertRaises(ValueError):
            verify_active(self.store)

    def primary_and_recovery(self):
        stage(self.store, self.manifest)
        promote(self.store, self.generation)
        files = {k: v for k, v in self.store.objects.items() if not k.startswith("staging/")}
        return VersionedStore(files, "COMPLETED"), VersionedStore(files, "REPLICA")

    def test_restore_from_version_pinned_replicas_when_current_pointer_advances(self):
        primary, recovery = self.primary_and_recovery()
        fetch = primary.download_version

        def advance_pointer(key, path, limit, head):
            fetch(key, path, limit, head)
            if key == "control/active.json":
                for store in (primary, recovery):
                    store.versions[key]["v2"] = json.dumps({"generation": "b" * 64}).encode()
                    store.latest[key] = "v2"

        primary.download_version = advance_pointer
        result = verify_recovery(primary, recovery, sleep=self.fail)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["generation"], self.generation)
        self.assertEqual(result["source_pointer_version"], "v1")
        self.assertEqual(result["table_rows"]["intervals"], 1)
        self.assertEqual(primary.reads, recovery.reads)
        self.assertEqual({v for _, v in recovery.reads}, {"v1"})
        self.assertEqual(len(recovery.reads), 3)

    def test_aws_adapter_reads_only_the_exact_version_and_checks_its_identity(self):
        adapter = S3("prod", "123456789012", recovery=True)
        self.assertEqual(adapter.bucket, "soccer-archive-prod-recovery-123456789012")
        target = self.root / "readback"
        head = {"ContentLength": 2, "VersionId": "version-a", "ETag": '"etag-a"'}
        def get(operation, key, *args):
            self.assertEqual((operation, key), ("get-object", "control/active.json"))
            self.assertEqual(args, ("--version-id", "version-a", "--if-match", '"etag-a"', str(target)))
            target.write_bytes(b"{}")
            return dict(head, VersionId="version-b")
        with patch.object(adapter, "call", side_effect=get):
            with self.assertRaisesRegex(ValueError, "selected version"):
                adapter.download_version("control/active.json", target, 2, head)
        with patch.object(adapter, "call") as call:
            with self.assertRaisesRegex(ValueError, "size limits"):
                adapter.download_version("control/active.json", target, 1, head)
            call.assert_not_called()

    def test_pending_replication_is_retryable_and_never_reported_as_verified(self):
        primary, recovery = self.primary_and_recovery()
        primary.status = "PENDING"
        delays = []
        with self.assertRaises(ReplicationPending):
            verify_recovery(primary, recovery, sleep=delays.append)
        self.assertTrue(delays)
        self.assertEqual(recovery.reads, [])
        def catch_up(_):
            primary.status = "COMPLETE"
        self.assertEqual(verify_recovery(primary, recovery, sleep=catch_up)["status"], "verified")

    def test_corrupt_recovery_data_and_failed_replication_fail_verification(self):
        primary, recovery = self.primary_and_recovery()
        key = f"generations/{self.generation}/archive.tar.gz"
        data = recovery.versions[key]["v1"]
        recovery.versions[key]["v1"] = data[:-1] + bytes([data[-1] ^ 1])
        with self.assertRaisesRegex(ValueError, "differ"):
            verify_recovery(primary, recovery, sleep=self.fail)
        recovery.versions[key]["v1"] = data
        for store, status in ((primary, "FAILED"), (recovery, "COMPLETE")):
            with self.subTest(status=status):
                prior = store.status
                store.status = status
                with self.assertRaisesRegex(ValueError, "replication status"):
                    verify_recovery(primary, recovery, sleep=self.fail)
                store.status = prior

    def test_corrupt_staging_cannot_be_activated_or_replaced(self):
        stage(self.store, self.manifest)
        key = f"staging/{self.generation}/archive.tar.gz"
        self.store.objects[key] = b"corrupt"
        with self.assertRaises(ValueError):
            promote(self.store, self.generation)
        with self.assertRaises(ValueError):
            stage(self.store, self.manifest)
        self.assertNotIn("control/active.json", self.store.objects)
        self.assertEqual(self.store.objects[key], b"corrupt")

    def test_wrong_generation_or_existing_pointer_cannot_be_promoted(self):
        stage(self.store, self.manifest)
        wrong = "b" * 64
        for name in ("manifest.json", "archive.tar.gz"):
            self.store.objects[f"staging/{wrong}/{name}"] = (self.manifest.parent / name).read_bytes()
        with self.assertRaises(ValueError):
            promote(self.store, wrong)
        self.store.objects["control/active.json"] = json.dumps({"generation": wrong}).encode()
        with self.assertRaises(ValueError):
            promote(self.store, self.generation)
        self.assertEqual(json.loads(self.store.objects["control/active.json"]), {"generation": wrong})
