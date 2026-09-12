import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import make_store  # noqa: E402

COLLECTOR = Path(__file__).resolve().parent.parent / "collector" / "hermes-usage.py"

sys.path.insert(0, str(COLLECTOR.parent))

import importlib.util
spec = importlib.util.spec_from_file_location("hermes_usage", COLLECTOR)
hu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hu)


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fixture_builds_record(self):
        make_store(self.root)
        # NOTE: constants below are defined in Task 2; this fails until then.
        self.assertTrue(hasattr(hu, "MAX_USAGE_ROWS"))

    def test_usage_scan_is_bounded(self):
        db = make_store(self.root, sessions=2, usage_per_session=2)
        conn = hu.connect(db)
        try:
            acc = hu.Accumulator()
            hu.scan_store(conn, acc)
            self.assertEqual(acc.usage_rows, 4)
            self.assertLessEqual(len(acc.tokens_by_model), hu.MAX_MODELS)
        finally:
            conn.close()

    def test_huge_model_name_is_truncated(self):
        db = make_store(self.root)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE session_model_usage SET model = ?", ("x" * 10000,))
        conn.commit()
        conn.close()
        conn = hu.connect(db)
        try:
            acc = hu.Accumulator()
            hu.scan_store(conn, acc)
            for name in acc.tokens_by_model:
                self.assertLessEqual(len(name), hu.MAX_MODEL_NAME_LEN)
        finally:
            conn.close()

    def test_today_counts_exact_with_capped_id_list(self):
        db = make_store(self.root, sessions=5)
        conn = hu.connect(db)
        try:
            acc = hu.Accumulator()
            hu.scan_store(conn, acc)
            self.assertEqual(acc.total_sessions, 5)
            self.assertGreaterEqual(acc.total_prompts, 5)
        finally:
            conn.close()

    def test_malformed_timestamps_do_not_crash(self):
        import time
        db = make_store(self.root, sessions=1, usage_per_session=1, messages_per_session=0)
        conn = sqlite3.connect(db)
        now = time.time()
        conn.execute("UPDATE session_model_usage SET first_seen = -5, last_seen = ?", (now * 1000 * 1000,))
        conn.commit()
        conn.close()
        conn = hu.connect(db)
        try:
            acc = hu.Accumulator()
            hu.scan_store(conn, acc)  # must not raise
            self.assertGreater(acc.usage_rows, 0)
        finally:
            conn.close()

    def test_record_respects_payload_ceiling(self):
        db = make_store(self.root, sessions=2)
        old_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.root)
        try:
            record = hu.build_record()
        finally:
            if old_env is None:
                del os.environ["HERMES_HOME"]
            else:
                os.environ["HERMES_HOME"] = old_env
        self.assertIsNotNone(record)
        payload = hu.serialize_record(record)
        self.assertLessEqual(len(payload.encode("utf-8")), hu.MAX_RECORD_BYTES)

    def test_write_rejects_symlinked_usage_dir(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "usage"
        os.symlink(real, link)
        with self.assertRaises(OSError):
            hu.write_record({"probe": True}, target_dir=link)

    def test_write_roundtrip(self):
        target = self.root / "usage"
        path = hu.write_record({"schemaVersion": 1}, target_dir=target)
        self.assertTrue(path.is_file() and not path.is_symlink())
        import json
        self.assertEqual(json.loads(path.read_text())["schemaVersion"], 1)

    def test_provider_rollup_and_tier_label(self):
        db = make_store(self.root)  # fixture rows are openai-codex/subscription_included
        import time
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO session_model_usage
            (session_id, model, billing_provider, billing_mode, input_tokens, output_tokens,
             estimated_cost_usd, first_seen, last_seen)
            VALUES ('sess-0','other-model','deepseek','',10000,5000,0.77,?,?)""",
            (time.time() - 100, time.time()),
        )
        conn.commit()
        conn.close()
        old_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.root)
        try:
            record = hu.build_record()
        finally:
            if old_env is None:
                del os.environ["HERMES_HOME"]
            else:
                os.environ["HERMES_HOME"] = old_env
        self.assertEqual(record["scope"], "device")
        self.assertIn("openai-codex", record["providerUsage"])
        self.assertIn("deepseek", record["providerUsage"])
        self.assertEqual(record["tierLabel"], "DeepSeek usage")

    def test_tier_label_subscription_majority(self):
        make_store(self.root)  # all rows openai-codex/subscription_included
        old_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.root)
        try:
            record = hu.build_record()
        finally:
            if old_env is None:
                del os.environ["HERMES_HOME"]
            else:
                os.environ["HERMES_HOME"] = old_env
        self.assertEqual(record["tierLabel"], "Codex subscription")

    def test_stderr_is_capped(self):
        import io as _io
        buffer = _io.StringIO()
        capped = hu.CappedStderr(buffer, 10)
        capped.write("x" * 100)
        capped.write("y" * 100)
        capped.flush()
        self.assertEqual(buffer.getvalue(), "x" * 10)


if __name__ == "__main__":
    unittest.main()
