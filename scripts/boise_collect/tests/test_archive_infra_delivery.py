"""Verify the review boundary used by the archive infrastructure workflow."""

from copy import deepcopy
import unittest

from scripts.archive_infra_plan import reviewed_digest


def change(kind, name, actions, before, after):
    return {
        "address": f"{kind}.{name}", "type": kind, "mode": "managed",
        "change": {"actions": actions, "before": before, "after": after},
    }


def plan(*resources):
    return {"resource_changes": list(resources), "output_changes": {}, "errored": False,
            "variables": {"account_id": {"value": "111122223333"}}}


class ArchiveInfraDeliveryTests(unittest.TestCase):
    def test_approved_alarm_and_worker_updates_bind_their_exact_values(self):
        alarm = change("aws_cloudwatch_metric_alarm", "errors", ["update"], {"threshold": 2}, {"threshold": 1})
        worker = change("aws_lambda_function", "worker", ["update"],
                        {"role": "arn:worker", "image_uri": "old"},
                        {"role": "arn:worker", "image_uri": "new", "reserved_concurrent_executions": 1})
        reviewed = plan(alarm, worker)
        self.assertEqual(reviewed_digest(reviewed), reviewed_digest(plan(worker, alarm)))
        changed = deepcopy(reviewed)
        changed["resource_changes"][0]["change"]["after"]["threshold"] = 3
        self.assertNotEqual(reviewed_digest(reviewed), reviewed_digest(changed))

    def test_refuse_privileged_first_apply_destroy_replace_and_role_changes(self):
        for resource in (
            change("aws_iam_role_policy", "worker", ["update"], {}, {}),
            change("aws_s3_bucket_policy", "archive", ["update"], {}, {}),
            change("aws_lambda_function", "worker", ["create"], None, {"role": "arn:worker"}),
            change("aws_cloudwatch_metric_alarm", "errors", ["delete"], {}, None),
            change("aws_lambda_function", "worker", ["delete","create"], {"role": "old"}, {"role": "new"}),
            change("aws_lambda_function", "worker", ["update"], {"role": "old"}, {"role": "new"}),
        ):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                reviewed_digest(plan(resource))

    def test_reject_state_import_and_move_even_if_the_resource_is_unchanged(self):
        imported = change("aws_cloudwatch_metric_alarm", "errors", ["no-op"], {}, {})
        imported["change"]["importing"] = {"id": "other"}
        moved = change("aws_cloudwatch_metric_alarm", "errors", ["no-op"], {}, {})
        moved["previous_address"] = "aws_cloudwatch_metric_alarm.other"
        for resource in (imported, moved):
            with self.assertRaises(ValueError):
                reviewed_digest(plan(resource))

    def test_primary_lifecycle_may_only_expire_superseded_control_versions(self):
        control = {"id": "superseded-control-records", "status": "Enabled",
                   "filter": [{"prefix": "control/"}], "noncurrent_version_expiration": [{"noncurrent_days": 60}]}
        def lifecycle(rule):
            return plan(change("aws_s3_bucket_lifecycle_configuration", "archive", ["update"], {}, {"rule": [rule]}))
        self.assertTrue(reviewed_digest(lifecycle(control)))
        with self.assertRaises(ValueError):
            reviewed_digest(lifecycle({**control, "filter": [{"prefix": "journal/"}]}))
        with self.assertRaises(ValueError):
            reviewed_digest(lifecycle({**control, "expiration": [{"days": 1}]}))

    def test_reject_other_alarm_actions_and_more_than_one_writer(self):
        for field in ("alarm_actions", "ok_actions", "insufficient_data_actions"):
            resource = change("aws_cloudwatch_metric_alarm", "errors", ["update"], {},
                              {field: ["arn:aws:sns:us-west-2:111122223333:another-topic"]})
            example = plan(resource)
            with self.subTest(field=field), self.assertRaises(ValueError):
                reviewed_digest(example)
        resource = change("aws_lambda_function", "worker", ["update"], {"role": "arn:worker"},
                          {"role": "arn:worker", "reserved_concurrent_executions": 2})
        with self.assertRaises(ValueError):
            reviewed_digest(plan(resource))

    def test_reject_unknown_actions_and_preserve_the_activation_owned_schedule_state(self):
        bad = change("aws_scheduler_schedule", "daily", ["update"], {"state": "DISABLED"}, {"state": "ENABLED"})
        with self.assertRaises(ValueError):
            reviewed_digest(plan(bad))
        with self.assertRaises(ValueError):
            reviewed_digest({"errored": True})
