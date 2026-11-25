from contextlib import closing, redirect_stdout
import copy
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.boise_collect.collector import ORIGIN, Response, request_path
from scripts.boise_collect.bruno_client import BrunoClient
from scripts.boise_collect.hosted import ACTIVE, WORKER, Conflict, Generations, run, scope
from scripts.boise_collect.publication import export_generation, restore_generation
from scripts.boise_collect.report import write_report
from scripts.boise_collect.store import Store
from scripts.boise_collect.tests.test_collector import DAY, FixtureServer


class Objects:
    def __init__(self):
        self.data = {}
        self.files = {}
        self.sequence = 0
        self.after_write = None
        self.setting = "manual"
        self.download_count = 0
        self.lose_bundle_ack = False

    def mode(self):
        return self.setting

    def read(self, key):
        return copy.deepcopy(self.data[key])

    def status(self, key):
        return self.read(key)[0] if key in self.data else {"status": "idle"}

    def write(self, key, value, previous=None):
        old = self.data.get(key, (None, None))[1]
        if previous != old:
            raise Conflict
        self.sequence += 1
        version = str(self.sequence)
        self.data[key] = (copy.deepcopy(value), version)
        if self.after_write and self.after_write(key, value):
            self.after_write = None
            raise RuntimeError("lost response to committed write")
        return version

    def download(self, key, path, limit=None, *, exact=False):
        self.download_count += 1
        data = self.files[key]
        if limit is not None and (len(data) > limit or (exact and len(data) != limit)):
            raise ValueError("out of bounds")
        path.write_bytes(data)

    def publish_file(self, key, path):
        data = path.read_bytes()
        if self.files.setdefault(key, data) != data:
            raise ValueError("generation collision")
        if self.lose_bundle_ack and key.endswith("/archive.tar.gz"):
            self.lose_bundle_ack = False
            raise RuntimeError("lost bundle receipt")


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.objects = Objects()
        self.now = 1_800_000_000.0
        self.base_clock = self.now
        self.events = []
        self.jobs = 0
        self.responses = []
        self.release = "a" * 40
        self.source = self.root / "baseline"
        Store(self.source, ORIGIN).close()
        (self.source / "collector.lock").touch()
        write_report(self.source, self.source / "reports", now=1)
        self.admit = {"operation": "refresh", "through": DAY, "days": 1, "budget": 2}
        self.seed()

    def seed(self):
        manifest = export_generation(self.source, self.root / "initial-generations", self.release)
        self.initial = manifest.parent.name
        for name in ("manifest.json", "archive.tar.gz"):
            self.objects.publish_file(f"generations/{self.initial}/{name}", manifest.parent / name)
        old = self.objects.data.get(ACTIVE, (None, None))[1]
        self.objects.write(ACTIVE, {"generation": self.initial}, old)

    def advance(self, seconds):
        self.now += seconds

    def get(self, kind, key):
        self.events.append((kind, key))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    base_url = ORIGIN

    def invoke(self, event=None, remaining=lambda: 900, client=None):
        self.jobs += 1
        path = self.root / f"invocation-{self.jobs}"
        path.mkdir()
        return run(self.admit if event is None else event, self.objects,
                   Generations(self.objects, path, self.release), client or self, remaining,
                   lambda: self.now, self.advance)

    def empty_responses(self):
        # The day API returns the list of games at Boise. The catalog returns
        # the list of upcoming divisions.
        self.responses.extend([Response(200, b"[]"), Response(200, b'{"data":{}}')])

    def replayed_database(self):
        target = self.root / f"readback-{self.jobs}"
        target.mkdir()
        generation = self.objects.read(ACTIVE)[0]["generation"]
        manifest = target / "manifest.json"
        for name in ("manifest.json", "archive.tar.gz"):
            self.objects.download(f"generations/{generation}/{name}", target / name)
        restore_generation(manifest, target / "data")
        return Store(target / "data", ORIGIN)

    def test_reserve_budget_replay_extension_and_duplicate_occurrence(self):
        self.empty_responses()
        first = {**self.admit, "budget": 1}
        self.assertEqual(self.invoke(first)["status"], "paused_budget")
        self.assertEqual(self.invoke({"operation": "resume"})["spent"], 1)
        extension = {"operation": "resume", "job": "refresh-" + DAY, "request_id": "more", "additional_budget": 1}
        state = self.invoke(extension)
        self.assertEqual((state["status"], state["budget"], state["spent"]), ("completed", 2, 2))
        self.assertEqual(self.invoke(extension), state)
        self.assertEqual(self.invoke(first), state)
        self.assertEqual(self.events, [("catalog", "upcoming"), ("day", DAY)])
        with closing(self.replayed_database()) as store:
            self.assertEqual(store.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 2)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 2)

    def test_duplicate_paused_job_uses_no_restore_and_emits_one_attention_line(self):
        self.empty_responses()
        first = {**self.admit, "budget": 1}
        logs = io.StringIO()
        with redirect_stdout(logs):
            self.assertEqual(self.invoke(first)["status"], "paused_budget")
            count = self.objects.download_count
            self.assertEqual(self.invoke(first)["status"], "paused_budget")
            self.assertEqual(self.invoke({"operation":"resume"})["status"], "paused_budget")
        self.assertEqual(self.objects.download_count, count)
        self.assertEqual([json.loads(line) for line in logs.getvalue().splitlines()],
                         [{"event": "archive_attention", "reason": "paused_budget"}])

    def test_completed_local_job_same_date_keeps_its_identity_and_scope(self):
        with closing(Store(self.source, ORIGIN)) as store:
            store.begin_refresh("refresh-" + DAY, date(2010, 9, 13), date.fromisoformat(DAY), True, 1)
            with store.conn:
                store.conn.execute("UPDATE refresh_job_resources SET status='skipped',skip_reason='invalid'")
            store.finish_refresh("refresh-" + DAY, 2)
        write_report(self.source, self.source / "reports", now=3)
        self.seed()
        self.empty_responses()
        self.assertEqual(self.invoke()["status"], "completed")
        with closing(self.replayed_database()) as store:
            stamps = [tuple(r) for r in store.conn.execute("SELECT id,start_day FROM refresh_jobs ORDER BY id")]
            self.assertEqual(stamps, [("aws-refresh-" + DAY, DAY), ("refresh-" + DAY, "2010-09-13")])

    def test_unfinished_imported_job_prevents_admission(self):
        with closing(Store(self.source, ORIGIN)) as store:
            store.begin_refresh("local-in-progress", date.fromisoformat(DAY), date.fromisoformat(DAY), True, 1)
        write_report(self.source, self.source / "reports", now=3)
        self.seed()
        with self.assertRaisesRegex(ValueError, "unfinished source"):
            self.invoke()
        self.assertEqual(self.invoke({"operation":"status"}), {"status": "idle"})
        self.assertEqual(self.events, [])

    def test_typo_future_date_cannot_advance_high_water(self):
        with self.assertRaisesRegex(ValueError, "in the future"):
            self.invoke({**self.admit, "through": "2099-01-31"})
        self.assertNotIn(WORKER, self.objects.data)

    def test_lost_bundle_receipt_reuses_immutable_content_across_later_invocations(self):
        self.empty_responses()
        self.objects.lose_bundle_ack = True
        with self.assertRaisesRegex(RuntimeError, "lost bundle receipt"):
            self.invoke()
        keys_after_lost_write = {k for k in self.objects.files if k.endswith("/archive.tar.gz")}
        self.assertEqual(len(keys_after_lost_write), 2)
        self.advance(600)
        self.assertEqual(self.invoke({"operation":"resume"})["status"], "completed")
        self.assertEqual({k for k in self.objects.files if k.endswith("/archive.tar.gz")}, keys_after_lost_write)
        self.assertEqual(len(self.events), 2)

    def test_crash_after_journal_before_control_ack_replays_once(self):
        self.objects.after_write = lambda key, value: key.startswith("journal/")
        self.empty_responses()
        with self.assertRaisesRegex(RuntimeError, "lost response"):
            self.invoke()
        self.advance(60)
        self.assertEqual(self.invoke({"operation": "resume"})["status"], "completed")
        self.assertEqual(len(self.events), 2)
        with closing(self.replayed_database()) as store:
            self.assertEqual(store.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 2)

    def test_crash_before_durable_reply_requires_an_explicit_resume_and_stays_charged(self):
        self.responses.append(RuntimeError("lost process after HTTP"))
        self.empty_responses()
        with self.assertRaisesRegex(RuntimeError, "lost process"):
            self.invoke({**self.admit, "budget": 3})
        self.assertEqual(self.invoke({"operation": "resume"})["spent"], 1)
        self.advance(60)
        self.assertEqual(self.invoke({"operation": "resume"})["status"], "interrupted")
        self.assertEqual(self.invoke({**self.admit, "budget": 3})["status"], "interrupted")
        self.assertEqual(self.invoke({"operation": "resume"})["status"], "completed")
        with closing(self.replayed_database()) as store:
            self.assertEqual([r[0] for r in store.conn.execute("SELECT outcome FROM attempts ORDER BY id")],
                             ["interrupted", "success", "success"])

    def test_failed_pointer_response_does_not_repeat_request_or_replace_generation(self):
        self.empty_responses()
        self.objects.after_write = lambda key, value: key == ACTIVE
        with self.assertRaisesRegex(RuntimeError, "lost response"):
            self.invoke()
        generation = self.objects.read(ACTIVE)[0]["generation"]
        state = self.invoke({"operation": "resume"})
        self.assertEqual((state["status"], state["generation"]), ("completed", generation))
        self.assertEqual(len(self.events), 2)

    def test_stale_base_refuses_more_requests(self):
        self.empty_responses()
        self.invoke({**self.admit, "budget": 1})
        self.objects.write(ACTIVE, {"generation": "b" * 64}, self.objects.read(ACTIVE)[1])
        with self.assertRaises(Conflict):
            self.invoke({"operation": "resume"})
        self.assertEqual(len(self.events), 1)

    def test_long_retry_after_survives_a_new_invocation(self):
        self.responses.append(Response(429, b"ignored", "300"))
        self.empty_responses()
        self.assertEqual(self.invoke({**self.admit, "budget": 3})["status"], "waiting")
        self.advance(250)
        self.assertEqual(self.invoke({"operation": "resume"})["spent"], 1)
        self.assertEqual(len(self.events), 1)
        self.advance(50)
        self.assertEqual(self.invoke({"operation": "resume"})["status"], "completed")
        self.assertEqual(len(self.events), 3)

    def test_failed_source_gaps_and_authentication_blocks(self):
        self.responses.extend([Response(200, b"[]"), Response(200, b'{"private":"never-retained"}')])
        result = self.invoke()
        self.assertEqual((result["status"], result["gaps"]), ("completed_with_gaps", 1))
        self.assertNotIn("never-retained", json.dumps(self.objects.data))
        with closing(self.replayed_database()) as store:
            self.assertEqual(store.conn.execute("SELECT status FROM resources WHERE kind='day'").fetchone()[0], "invalid")
        self.responses.append(Response(403, b"secret"))
        tomorrow = {**self.admit, "through": "2010-09-27"}
        self.assertEqual(self.invoke(tomorrow)["status"], "blocked")
        self.assertEqual(self.invoke({"operation": "resume"})["status"], "blocked")
        self.assertEqual(len(self.events), 3)

    def test_explicitly_retry_a_blocked_request(self):
        self.responses.extend([Response(401, b"denied"), Response(200, b"[]"), Response(200, b'{"data":{}}')])
        self.assertEqual(self.invoke({**self.admit, "budget": 3})["status"], "blocked")
        retry = {"operation": "resume", "job": "refresh-" + DAY, "retry_blocked_attempt": 1}
        self.assertEqual(self.invoke(retry)["status"], "completed")
        self.assertEqual(self.invoke(retry)["spent"], 3)
        with closing(self.replayed_database()) as store:
            self.assertEqual([r[0] for r in store.conn.execute("SELECT outcome FROM attempts ORDER BY id")],
                             ["blocked", "success", "success"])

    def test_collection_mode_gates_manual_scheduled_and_each_upstream_call(self):
        self.objects.setting = "off"
        self.assertEqual(self.invoke()["status"], "collection_disabled")
        self.assertNotIn(WORKER, self.objects.data)
        self.objects.setting = "manual"
        self.assertEqual(self.invoke({"operation":"scheduled", "scheduled_time":"2026-11-25T12:00:00Z"})["status"],
                         "collection_disabled")
        self.assertNotIn(WORKER, self.objects.data)
        self.empty_responses()

        def stop_after_request(key, state):
            if key.startswith("journal/"):
                self.objects.setting = "off"
            return False

        self.objects.after_write = stop_after_request
        self.assertEqual(self.invoke()["status"], "collection_disabled")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.invoke({"operation": "status"})["spent"], 1)
        self.objects.after_write = None
        self.objects.setting = "daily"
        self.assertEqual(self.invoke({"operation": "resume"})["status"], "completed")

    def test_known_gap_survives_without_refetch(self):
        with closing(Store(self.source, ORIGIN)) as store:
            target = date.fromisoformat(DAY)
            store.queue_interval(target, target)
            attempt = store.start_attempt("day", DAY, 1)
            store.fail(attempt, "invalid", 200, "invalid_source_response", "a"*64, None, 2)
        write_report(self.source, self.source / "reports", now=3)
        self.seed()
        self.responses.append(Response(200, b"[]"))
        result = self.invoke()
        self.assertEqual((result["status"], result["gaps"]), ("completed_with_gaps", 1))
        self.assertEqual(self.events, [("catalog", "upcoming")])

    def test_deadline_does_not_start_a_request_and_status_is_read_only(self):
        self.assertEqual(self.invoke({"operation": "status"}), {"status": "idle"})
        self.assertNotIn(WORKER, self.objects.data)
        self.assertEqual(self.invoke(remaining=lambda: 10)["spent"], 0)
        self.assertEqual(self.events, [])

    def test_event_allowlist_and_boise_time(self):
        op, params = scope({"operation":"scheduled", "scheduled_time":"2026-11-25T06:59:00Z"})
        self.assertEqual((op, params["through"]), ("scheduled", "2026-11-24"))
        for event in ({**self.admit, "url": "https://other.invalid"}, {**self.admit, "days": 100},
                      {**self.admit, "budget": True}, {"operation": "delete"}):
            with self.assertRaises(ValueError):
                self.invoke(event)

    def test_real_transport_with_loopback_and_private_fields_filtered(self):
        routes = {request_path("catalog", "upcoming"): (200, [], {}),
                  request_path("day", DAY): (200, {"data": {}}, {})}
        with FixtureServer(routes) as server:
            transport = BrunoClient(server.origin)
            # The test pipes the port-bound loopback transport into the same
            # collector that validates the staged production-origin manifest.
            class LocalTransport:
                base_url = ORIGIN
                get = staticmethod(transport.get)

            self.assertEqual(self.invoke(client=LocalTransport())["status"], "completed")
            self.assertEqual(len(server.calls), 2)
            for _, headers in server.calls:
                self.assertTrue({"authorization", "cookie"}.isdisjoint(key.lower() for key in headers))


class TransferBoundsTests(unittest.TestCase):
    def test_json_documents_and_manifest_declared_bytes_are_bounded_before_read(self):
        from scripts.boise_collect.hosted_storage import S3Objects

        class Body(io.BytesIO):
            def read(self, *args):
                raise AssertionError("object exceeds allowed length")

        class Client:
            def get_object(self, **kwargs):
                return {"Body": Body(), "ContentLength": 2 * 1024**2, "ETag": "etag"}

        objects = S3Objects(Client(), "fixture")
        with self.assertRaisesRegex(ValueError, "size limit"):
            objects.read(WORKER)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "transfer length"):
                objects.download("generations/fixture/archive.tar.gz", Path(tmp) / "archive", 8, exact=True)
