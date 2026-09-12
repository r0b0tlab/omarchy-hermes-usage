"""Local fixtures only; upstream SQL is read as AST, never imports auth."""
import ast
import json
import sqlite3
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from test_collector import hu, CollectorTest


class DetailsTest(CollectorTest):
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
