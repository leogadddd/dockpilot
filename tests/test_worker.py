import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dockpilot import worker


class TestWorkerRecovery(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.task_id = "a" * 24
        (root / "queue").mkdir()
        staging = root / "staging" / self.task_id
        staging.mkdir(parents=True)
        self.media = staging / "photo.jpg"
        self.media.write_bytes(b"staged data")
        env = root / "immich.env"
        env.touch(mode=0o600)
        self.config = root / "config.yaml"
        self.config.write_text(json.dumps({
            "version": 1,
            "targets": {"immich-main": {"type": "immich", "allowed_actions": ["immich.import"]}},
            "worker": {
                "enabled": True,
                "shared_root": str(root),
                "immich_env_file": str(env),
                "max_retries": 1,
                "retry_delay_seconds": 120,
            },
        }))
        self.item = root / "queue" / (self.task_id + ".json")
        self.failed = self.item.with_suffix(".failed")
        self.receipt = root / "receipts" / self.item.name
        self.record = {
            "version": 1,
            "id": self.task_id,
            "staging_id": self.task_id,
            "action": "immich.import",
            "target": "immich-main",
        }
        self.item.write_text(json.dumps(self.record))
        self.enterContext(patch.object(worker, "LOCK_PATH", root / "worker.lock"))
        self.enterContext(patch.object(worker, "notify"))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.upload = self.enterContext(patch.object(worker, "upload"))
        self.clock = self.enterContext(patch.object(worker.time, "time", return_value=1000))

    def test_stop_keeps_job_for_next_run(self):
        self.upload.side_effect = worker.ImportInterrupted
        self.assertEqual(worker.run_once(self.config), 0)
        self.assertEqual(json.loads(self.item.read_text()), self.record)
        self.assertEqual(self.media.read_bytes(), b"staged data")
        self.assertFalse(self.failed.exists())
        self.assertFalse(self.receipt.exists())

        self.upload.side_effect = None
        self.assertEqual(worker.run_once(self.config), 0)
        self.assertFalse(self.item.exists())
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "success")

    def test_persisted_backoff_exhaustion_and_manual_retry(self):
        self.upload.side_effect = worker.UploadError("connection closed")
        self.assertEqual(worker.run_once(self.config), 0)
        retry = json.loads(self.item.read_text())["worker_retry"]
        self.assertEqual(retry, {"failures": 1, "not_before": 1120})

        self.clock.return_value = 1119
        self.assertEqual(worker.run_once(self.config), 0)
        self.assertEqual(self.upload.call_count, 1)

        self.clock.return_value = 1120
        self.assertEqual(worker.run_once(self.config), 1)
        self.assertTrue(self.failed.exists())
        self.assertFalse(self.item.exists())

        self.assertEqual(worker.retry_failed(self.config, self.task_id), 0)
        self.assertEqual(json.loads(self.item.read_text()), self.record)
        self.assertFalse(self.failed.exists())

    def test_legacy_failed_job_can_be_requeued(self):
        self.item.rename(self.failed)
        self.assertEqual(worker.retry_failed(self.config, self.task_id), 0)
        self.assertEqual(json.loads(self.item.read_text()), self.record)

    def test_bad_job_is_not_retried(self):
        self.item.write_text("[]")
        self.assertEqual(worker.run_once(self.config), 1)
        self.upload.assert_not_called()
        self.assertTrue(self.failed.exists())
