"""Local fixtures only; upstream SQL is read as AST, never imports auth."""
import ast
import json
import sqlite3
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from test_collector import hu
from fixtures import make_store
import tempfile


class DetailsTest(TestCase):
    def test_bounds_and_partial_scans(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_store(Path(tmp), sessions=1, usage_per_session=100, messages_per_session=0)
            with sqlite3.connect(db) as c:
                for i in range(100):
                    c.execute('UPDATE session_model_usage SET model=?, billing_provider=?, task=? WHERE rowid=?',
                              (str(i), str(i), str(i), i + 1))
            c.close()
            conn = hu.connect(db); acc = hu.Accumulator()
            try:
                hu.scan_store(conn, acc)
            finally:
                conn.close()
            self.assertLessEqual(len(acc.today_tokens_by_model), hu.MAX_MODELS)
            self.assertLessEqual(len(acc.provider_tokens), hu.MAX_MODELS)
            self.assertLessEqual(len(acc.task_details), 32)
            conn = hu.connect(db); acc = hu.Accumulator()
            try:
                with patch.object(hu, 'MAX_USAGE_ROWS', 2): hu.scan_store(conn, acc)
            finally:
                conn.close()
            self.assertTrue(acc.truncated)

    def test_unknown_default_cost_zero_is_not_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_store(Path(tmp), sessions=1, usage_per_session=1)
            with sqlite3.connect(db) as c:
                c.execute('UPDATE session_model_usage SET estimated_cost_usd=0, actual_cost_usd=0, cost_status=NULL')
            c.close()
            c = hu.connect(db); acc = hu.Accumulator()
            try: hu.scan_store(c, acc)
            finally: c.close()
            self.assertIsNone(acc.details['estimatedUsd'])
            self.assertIsNone(acc.details['actualUsd'])

    def test_collector_rejects_writable_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / 'unsafe'; parent.mkdir(mode=0o777); parent.chmod(0o777)
            with self.assertRaises(OSError): hu.write_record({'id': 'hermes'}, parent / 'usage')

    def test_corrupt_store_reports_partial_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp); (home/'state.db').write_bytes(b'not sqlite')
            with patch.dict(hu.os.environ, HERMES_HOME=str(home)):
                record=hu.build_record()
            self.assertTrue(record['details']['truncated'])

    def test_stderr_caps_utf8_bytes(self):
        import io
        stream=io.StringIO(); capped=hu.CappedStderr(stream,10)
        capped.write('é'*100)
        self.assertLessEqual(len(stream.getvalue().encode()),10)

    def test_old_session_time_columns(self):
        c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row
        c.execute('CREATE TABLE sessions(id, started_at)')
        c.execute("INSERT INTO sessions VALUES ('s', ?)",(time.time(),))
        acc=hu.Accumulator()
        try: hu.scan_store(c,acc)
        finally: c.close()
        self.assertEqual(acc.today_sessions,1)
        self.assertFalse(acc.truncated)

    def test_ordinary_title_and_vision_groups_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=make_store(Path(tmp),sessions=1,usage_per_session=3)
            c=sqlite3.connect(db)
            for i,(task,calls,provider) in enumerate((('',2,'nous'),('title_generation',3,'nous'),('vision',1,'openrouter')),1):
                c.execute('UPDATE session_model_usage SET task=?,api_call_count=?,billing_provider=? WHERE rowid=?',(task,calls,provider,i))
            c.commit(); c.close()
            c=hu.connect(db); acc=hu.Accumulator()
            try: hu.scan_store(c,acc)
            finally: c.close()
            self.assertEqual(set(acc.task_details),{'ordinary','title_generation','vision'})
            for key in ('calls','tokens'):
                self.assertEqual(sum(v[key] for v in acc.task_details.values()),acc.details[key])
                self.assertEqual(sum(v[key] for v in acc.provider_details.values()),acc.details[key])

    def test_profile_enumeration_and_message_group_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp); (home/'profiles').mkdir()
            for i in range(6):
                folder=home/'profiles'/str(i); folder.mkdir(); (folder/'state.db').touch()
            acc=hu.Accumulator()
            with patch.dict(hu.os.environ,HERMES_HOME=str(home)), patch.object(hu,'MAX_PROFILE_ENTRIES',3):
                self.assertLessEqual(len(hu.store_paths(acc)),3)
                self.assertTrue(acc.truncated)
            db=make_store(home,sessions=5,messages_per_session=2)
            c=hu.connect(db); acc=hu.Accumulator()
            try:
                with patch.object(hu,'MAX_DAY_GROUPS',2):
                    weights=hu.message_day_weights(c,['sess-'+str(i) for i in range(5)],acc)
                self.assertLessEqual(sum(len(v) for v in weights.values()),2)
                self.assertTrue(acc.truncated)
            finally: c.close()

    def test_stdout_uses_final_serializer_ceiling(self):
        from contextlib import redirect_stdout
        import io
        output=io.StringIO()
        with patch.object(hu,'build_record',return_value={'details':{'huge':'x'*(hu.MAX_RECORD_BYTES+1)}}),redirect_stdout(output),patch.object(hu.sys,'stderr',io.StringIO()):
            self.assertEqual(hu.main([]),1)
        self.assertEqual(output.getvalue(),'')

    def test_serializer_final_cap(self):
        with self.assertRaises(ValueError):
            hu.serialize_record({'details': {'huge': 'x' * (hu.MAX_RECORD_BYTES + 1)}})

    def test_sql_budget_counts_vm_ops(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_store(Path(tmp))
            with patch.object(hu, 'SQLITE_OP_BUDGET', 10000):
                c = hu.connect(db)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        c.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n').fetchone()
                finally:
                    c.close()

    def test_history_not_current_plan(self):
        acc = hu.Accumulator()
        acc.provider_tokens['openai-codex'] = 100
        acc.provider_sub_tokens['openai-codex'] = 100
        self.assertEqual(hu.describe_plan(acc), 'Historical provider mix')

    def test_mixed_status_real_hermes_upsert(self):
        source = Path('/home/r0b0tmagic/.hermes/hermes-agent/hermes_state_usage.py')
        tree = ast.parse(source.read_text())
        sql = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '_MODEL_USAGE_UPSERT_SQL' for t in n.targets))
        c = sqlite3.connect(':memory:')
        c.row_factory = sqlite3.Row
        c.execute('''CREATE TABLE session_model_usage(session_id, model, billing_provider,
          billing_base_url, billing_mode, task, api_call_count, input_tokens, output_tokens,
          cache_read_tokens, cache_write_tokens, reasoning_tokens, estimated_cost_usd,
          actual_cost_usd, cost_status, cost_source, first_seen, last_seen,
          UNIQUE(session_id, model, billing_provider, billing_base_url, billing_mode, task))''')
        base = ('s', 'model', 'nous', '', '', '', 1, 100, 50, 0, 0, 40)
        c.execute(sql, (*base, .30, 0, 'estimated', '', time.time(), time.time()))
        c.execute(sql, (*base, 0, .10, 'actual', '', time.time(), time.time()))
        acc = hu.Accumulator()
        hu.scan_store(c, acc)
        c.close()
        self.assertTrue(hasattr(acc, 'details'), 'detail aggregates missing')
        self.assertAlmostEqual(acc.details['estimatedUsd'], .30)
        self.assertAlmostEqual(acc.details['actualUsd'], .10)
        self.assertEqual(acc.details['calls'], 2)
        self.assertEqual(acc.details['latestStatusRows']['actual'], 1)
        self.assertEqual(acc.details['tokens'], 300)
        self.assertEqual(acc.details['reasoning'], 80)
        self.assertEqual(acc.task_details['ordinary']['calls'], 2)

    def test_old_schema_unknown_not_zero(self):
        c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
        c.execute('CREATE TABLE session_model_usage(session_id, model, input_tokens, output_tokens)')
        c.execute("INSERT INTO session_model_usage VALUES ('s','old',10,20)")
        acc = hu.Accumulator(); hu.scan_store(c, acc); c.close()
        self.assertEqual(acc.usage_rows, 1)
        self.assertIsNone(acc.details['calls'])
        self.assertIsNone(acc.details['estimatedUsd'])
        self.assertIn('unknown', acc.task_details)

    def test_cost_rejects_non_numbers_and_overflow(self):
        self.assertTrue(hasattr(hu, 'finite_cost'), 'cost validator missing')
        for value in (True, False, float('inf'), float('nan'), 10**10000, -1, '1.2', None):
            self.assertIsNone(hu.finite_cost(value))
        self.assertEqual(hu.finite_cost(0), 0)
