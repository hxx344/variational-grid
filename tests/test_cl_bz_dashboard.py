"""Real independent QQQ and CL/BZ ledgers behind the shared HTTP and Hub service."""
from contextlib import closing, ExitStack
from dataclasses import replace
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from variational_grid.cl_bz_scalper import (
    ScalperCohort, ScalperConfig, ScalperExperiment, ScalperFrame, read_history, read_snapshot,
)
from variational_grid.dashboard import make_server
from variational_grid.models import D, GridError, utc
from variational_grid.qqq_comparison import QQQCohort, QQQExperiment
from variational_grid.qqq_hedge import QQQConfig, QQQSettings
from variational_grid.qqq_pricing import QQQPricing
from variational_grid.reset import process_reset, read_state, save_state
from test_qqq import market, quote, trade


class CLBZDashboardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = int(time.time())
        base = SimpleNamespace(poll_seconds=2, session_file=str(self.root / "private/session.json"))
        qqq_output, cl_output = self.root / "qqq-paper", self.root / "cl-bz-paper"
        settings = QQQSettings(grid_count=2)
        qqq_config = QQQConfig(settings, "qqq-original", "1", "2", str(qqq_output / "ledgers/qqq.sqlite3"))
        self.primary = QQQExperiment(base, qqq_output, {"qqq-original": qqq_config}, settings,
                                     QQQPricing(mode="exact_quantity"))
        cl_config = ScalperConfig(state_file=str(cl_output / "ledgers/cl.sqlite3"))
        self.companion = ScalperExperiment(base, cl_output, {"cl-long-bz-hedge": cl_config}, ())
        self.qqq = self.stack.enter_context(QQQCohort(self.primary))
        self.cl = self.stack.enter_context(ScalperCohort(self.companion))
        for ts, trades in ((self.now - 20, ()), (self.now - 10, (trade(1, self.now - 11),))):
            frame = self.qqq.prepare_frame(ts, {"lighter": market(ts, trades), "var": quote(ts),
                                               "allow_entries": True, "reason": ""}, data_kind="synthetic")
            for name, plan in frame.plans.items():
                amount = abs(D(plan["target"]) - D(self.qqq.stores[name].account()["us100"]["qty"]))
                if amount >= D(".000004"):
                    frame.quotes[format(amount.normalize(), "f")] = quote(ts, amount)
            self.qqq.ingest(frame)
        self.cl.ingest(self.frame(self.now - 20))
        self.cl.ingest(self.frame(self.now - 10, cl="69.98"))
        self.server = make_server(self.primary, 0, convergence=self.companion)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server, self.server, self.thread)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    @staticmethod
    def close_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    @staticmethod
    def frame(ts, cl="70", bz="74", paused=False):
        markets = {symbol: {"state": "closed" if paused and symbol == "CL" else "open",
                            "source_ts": ts, "closes_at": None} for symbol in ("CL", "BZ")}
        quotes = {} if paused else {symbol: {"ts": ts, "qty": "1", "bid": str(D(mark) - D(".01")),
                                           "ask": str(D(mark) + D(".01")), "mark": mark}
                                    for symbol, mark in (("CL", cl), ("BZ", bz))}
        return ScalperFrame(ts, markets, quotes, data_kind="synthetic")

    def request(self, path, method="GET", body=None, headers=None, server=None):
        target = server or self.server
        with closing(http.client.HTTPConnection("127.0.0.1", target.server_port, timeout=3)) as connection:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()

    def json(self, path, **kwargs):
        status, _, raw = self.request(path, **kwargs)
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    def test_shared_service_keeps_primary_page_and_serves_only_enabled_strategy_routes(self):
        catalog = self.json("/api/strategies")["strategies"]
        self.assertEqual([(r["id"], r["url"]) for r in catalog], [("primary", "/"), ("cl-bz", "/cl-bz")])
        self.assertIn("剥头皮", catalog[1]["label"])
        for path in ("/", "/cl-bz", "/convergence.js", "/convergence.css", "/strategies.js", "/var-session.js"):
            with self.subTest(path=path):
                status, headers, raw = self.request(path)
                self.assertEqual(status, 200)
                self.assertTrue(raw)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertIn(b"/qqq.js", self.request("/")[2])
        self.assertNotIn(b"/convergence.js", self.request("/")[2])
        self.assertIn(b"/convergence.js", self.request("/cl-bz")[2])
        self.assertEqual(self.request("/cl-bz", "HEAD")[2], b"")
        self.assertEqual(self.request("/api/cl-bz-snapshot", headers={"Host": "attacker.invalid"})[0], 403)
        standalone = make_server(self.primary, 0)
        worker = threading.Thread(target=standalone.serve_forever, daemon=True)
        worker.start()
        try:
            catalog = self.json("/api/strategies", server=standalone)["strategies"]
            self.assertEqual([r["id"] for r in catalog], ["primary"])
            for path in ("/cl-bz", "/api/cl-bz-snapshot", "/api/cl-bz-history?range=24h", "/convergence.js", "/convergence.css"):
                self.assertEqual(self.request(path, server=standalone)[0], 404, path)
            self.assertEqual(self.request("/strategies.js", server=standalone)[0], 200)
        finally:
            self.close_server(standalone, worker)

    def test_snapshots_read_real_separate_ledgers_without_a_history_scan(self):
        with patch("variational_grid.cl_bz_scalper.read_history", side_effect=AssertionError("No history scan")), \
                patch("variational_grid.client.Client.request", side_effect=AssertionError("No venue read")):
            cl = self.json("/api/cl-bz-snapshot")
            qqq = self.json("/api/qqq-snapshot")
        self.assertEqual(cl["kind"], "cl_bz_scalper")
        self.assertEqual(cl["summary"]["mode"], "cl_bz_scalper")
        self.assertEqual(cl["summary"]["ts"], self.now - 10)
        self.assertTrue(cl["details_available"])
        self.assertEqual(len(cl["positions"]), 1)
        self.assertEqual(D(cl["positions"][0]["qty"]), 1)
        self.assertEqual(D(cl["positions"][0]["tp_price"]), D(cl["positions"][0]["entry_cl"]) * D("1.001"))
        self.assertEqual({o["side"] for o in cl["orders"]}, {"sell"})
        self.assertEqual(cl["parameters"]["take_profit_percent"], "0.1")
        self.assertEqual(qqq["summary"]["kind"], "qqq_hedge")
        self.assertNotEqual(qqq["reset_token"], cl["reset_token"])
        self.assertNotEqual(qqq["reset"]["generation"], cl["reset"]["generation"])
        self.assertEqual(cl["var_session"]["enabled"], qqq["var_session"]["enabled"])
        self.assertNotIn("pid", cl["runtime"])
        self.assertNotIn(str(self.root), json.dumps(cl))

    def test_history_is_bounded_by_snapshot_time_and_rejects_unrecognized_query_fields(self):
        cl = self.json("/api/cl-bz-snapshot")
        through = cl["summary"]["ts"]
        self.cl.ingest(self.frame(self.now, cl="70.20", bz="75"))
        historical = self.json(f"/api/cl-bz-history?range=1h&through={through}")
        self.assertEqual(historical["summary_ts"], through)
        self.assertEqual(historical["reset_generation"], cl["reset"]["generation"])
        self.assertEqual(historical["history"]["names"], ["cl-long-bz-hedge"])
        self.assertEqual(historical["history"]["source_count"], 2)
        self.assertEqual(max(p["ts"] for p in historical["history"]["points"]), through)
        self.assertEqual(self.json("/api/cl-bz-snapshot")["summary"]["ts"], self.now)
        for path in ("/api/cl-bz-snapshot?range=1h", "/api/cl-bz-history?range=30d",
                     "/api/cl-bz-history?through=NaN", "/api/cl-bz-history?through=Infinity",
                     "/api/cl-bz-history?through=-1", "/api/cl-bz-history?range=1h&strategy=qqq",
                     "/api/cl-bz-history?path=../../qqq-paper/comparison.sqlite3"):
            self.assertEqual(self.request(path)[0], 400, path)
        self.assertEqual(self.request("/api/cl-bz-history?range=7d", "HEAD")[2], b"")

    def test_slow_actual_history_query_cannot_block_fast_snapshot_or_primary_account(self):
        entered, release = threading.Event(), threading.Event()
        original_connect = sqlite3.connect

        class DelayedConnection(sqlite3.Connection):
            def execute(connection, sql, *args, **kwargs):
                if "ROW_NUMBER() OVER (ORDER BY ts)" in sql and not entered.is_set():
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("Snapshot waited for history")
                return super().execute(sql, *args, **kwargs)

        def connect(*args, **kwargs):
            return original_connect(*args, **{**kwargs, "factory": DelayedConnection})

        result = []
        worker = threading.Thread(target=lambda: result.append(self.request("/api/cl-bz-history?range=24h")))
        try:
            with patch("sqlite3.connect", side_effect=connect):
                worker.start()
                self.assertTrue(entered.wait(2))
                cl_status, _, cl_raw = self.request("/api/cl-bz-snapshot")
                qqq_status, _, qqq_raw = self.request("/api/qqq-snapshot")
                release.set()
                worker.join(4)
            self.assertEqual(cl_status, 200, cl_raw)
            self.assertEqual(qqq_status, 200, qqq_raw)
            self.assertEqual(json.loads(cl_raw)["summary"]["ts"], self.now - 10)
            self.assertEqual(result[0][0], 200, result)
        finally:
            release.set()
            if worker.ident is not None:
                worker.join(5)

    def test_reset_token_generation_origin_and_json_shape_are_scoped_to_companion(self):
        cl, qqq = self.json("/api/cl-bz-snapshot"), self.json("/api/qqq-snapshot")
        body = json.dumps({"generation": cl["reset"]["generation"]})
        headers = {"Origin": self.origin, "Content-Type": "application/json", "X-Reset-Token": cl["reset_token"]}
        before = read_state(self.companion)
        for changed in ({"Origin": None}, {"Origin": "https://attacker.invalid"},
                        {"Origin": "http://localhost:" + str(self.server.server_port)},
                        {"X-Reset-Token": qqq["reset_token"]}, {"X-Reset-Token": None},
                        {"Sec-Fetch-Site": "cross-site"}, {"Host": "attacker.invalid"}):
            invalid = {key: value for key, value in {**headers, **changed}.items() if value is not None}
            self.assertEqual(self.request("/api/cl-bz-reset", "POST", body, invalid)[0], 403, changed)
        self.assertEqual(read_state(self.companion), before)
        self.assertEqual(self.request("/api/reset", "POST", json.dumps({"generation": qqq["reset"]["generation"]}), headers)[0], 403)
        self.assertEqual(self.request("/api/cl-bz-reset", "POST", json.dumps({"generation": qqq["reset"]["generation"]}), headers)[0], 409)
        for invalid in ("not-json", "[]", json.dumps({"generation": before["generation"], "strategy": "primary"})):
            self.assertEqual(self.request("/api/cl-bz-reset", "POST", invalid, headers)[0], 400)
        self.assertEqual(self.request("/api/cl-bz-reset", "POST", body, {**headers, "Content-Type": "text/plain"})[0], 400)
        self.assertEqual(read_state(self.companion), before)
        status, _, raw = self.request("/api/cl-bz-reset", "POST", body, headers)
        self.assertEqual(status, 202, raw)
        self.assertEqual(json.loads(raw)["reset"]["status"], "pending")
        self.assertEqual(read_state(self.primary), qqq["reset"])

    def test_real_companion_archive_and_reset_preserves_primary_ledger_and_orders(self):
        cl, qqq = self.json("/api/cl-bz-snapshot"), self.json("/api/qqq-snapshot")
        qqq_account = next(iter(self.qqq.stores.values())).account()
        headers = {"Origin": self.origin, "Content-Type": "application/json", "X-Reset-Token": cl["reset_token"]}
        self.assertEqual(self.request("/api/cl-bz-reset", "POST", json.dumps({"generation": cl["reset"]["generation"]}), headers)[0], 202)
        self.assertTrue(process_reset(self.cl))
        updated = self.json("/api/cl-bz-snapshot")
        self.assertIsNone(updated["summary"])
        self.assertEqual(updated["positions"], [])
        self.assertEqual(updated["orders"], [])
        self.assertNotEqual(updated["reset"]["generation"], cl["reset"]["generation"])
        archive = self.companion.output / "archives" / updated["reset"]["archive_id"]
        self.assertTrue((archive / "complete.json").is_file())
        self.assertTrue((archive / "ledgers/cl-long-bz-hedge.sqlite3").is_file())
        after = self.json("/api/qqq-snapshot")
        for key in ("summary", "positions", "trades", "reset", "reset_token"):
            self.assertEqual(after[key], qqq[key], key)
        self.assertEqual(next(iter(self.qqq.stores.values())).account(), qqq_account)
        self.assertEqual(self.json("/api/cl-bz-history?range=24h")["history"]["points"], [])

    def test_strict_reset_origin_does_not_accept_a_malformed_localhost_authority(self):
        snapshot = self.json("/api/cl-bz-snapshot")
        state = read_state(self.companion)
        body = json.dumps({"generation": state["generation"]})
        for host in ("localhost:bad", "localhost?x=1", "localhost#fragment", "user@localhost"):
            headers = {"Host": host, "Origin": "http://" + host, "Content-Type": "application/json",
                       "X-Reset-Token": snapshot["reset_token"]}
            with self.subTest(host=host):
                self.assertEqual(self.request("/api/cl-bz-reset", "POST", body, headers)[0], 403)
                self.assertEqual(read_state(self.companion), state)

    def test_hub_combines_independent_metrics_without_summing_accounts(self):
        cl, qqq = self.json("/api/cl-bz-snapshot"), self.json("/api/qqq-snapshot")
        with patch("variational_grid.cl_bz_scalper.read_snapshot", side_effect=AssertionError("No detail read")), \
                patch("variational_grid.client.Client.request", side_effect=AssertionError("No venue read")):
            summary = self.json("/api/hub/summary?schemaVersion=2")
        metrics = {row["key"]: row for row in summary["data"]["metrics"]}
        self.assertEqual(summary["schemaVersion"], 2)
        self.assertEqual(metrics["paper_pnl_1"]["value"], float(qqq["summary"]["scenarios"][0]["total_pnl_usdc"]))
        self.assertEqual(metrics["cl_bz_paper_pnl_1"]["value"], float(cl["summary"]["scenarios"][0]["total_pnl_usdc"]))
        self.assertEqual(metrics["paper_pnl_1"]["unit"], "USDC")
        self.assertEqual(metrics["cl_bz_paper_pnl_1"]["unit"], "USDC")
        self.assertEqual(metrics["cl_bz_sample_time"]["value"], utc(self.now - 10))
        self.assertIn("剥头皮", metrics["cl_bz_mode"]["value"])
        self.assertNotIn("total_balance", metrics)
        self.assertNotIn("reset_token", json.dumps(summary))

    def test_fresh_paused_frames_do_not_refresh_old_cl_bz_source_times_in_hub(self):
        source_time = self.now - 10
        self.cl.ingest(self.frame(self.now + 120, paused=True))
        cl = self.json("/api/cl-bz-snapshot")
        self.assertEqual(cl["summary"]["ts"], self.now + 120)
        self.assertEqual(cl["summary"]["market"]["cl_source_ts"], source_time)
        self.assertEqual(cl["summary"]["market"]["bz_source_ts"], source_time)
        self.assertEqual(cl["positions"][0]["valued_at"], source_time)
        with patch("variational_grid.hub.time.time", return_value=self.now + 120):
            combined = self.json("/api/hub/summary")["data"]
        metrics = {row["key"]: row for row in combined["metrics"]}
        self.assertEqual(metrics["cl_bz_sample_time"]["value"], utc(source_time))
        self.assertEqual(combined["updatedAt"], utc(source_time))
        self.assertEqual(combined["health"]["state"], "stale")

    def test_empty_and_resetting_companion_never_borrow_primary_data(self):
        output = self.root / "empty-cl"
        config = replace(self.companion.settings, state_file=str(output / "ledgers/cl.sqlite3"))
        empty = replace(self.companion, output=output, scenarios={"empty": config})
        before = read_snapshot(empty)
        self.assertIsNone(before["summary"])
        self.assertFalse(before["details_available"])
        self.assertEqual(read_history(empty, "24h")["history"]["points"], [])
        state = read_state(self.companion)
        save_state(self.companion, {**state, "status": "clearing"})
        try:
            snapshot = self.json("/api/cl-bz-snapshot")
            self.assertIsNone(snapshot["summary"])
            self.assertFalse(snapshot["details_available"])
            self.assertEqual(snapshot["positions"], [])
            history = self.json("/api/cl-bz-history?range=1h")
            self.assertEqual(history["reset_generation"], state["generation"])
            self.assertEqual(history["history"]["points"], [])
            self.assertIsNotNone(self.json("/api/qqq-snapshot")["summary"])
        finally:
            save_state(self.companion, state)

    def test_failures_are_private_and_one_history_failure_does_not_break_other_reads(self):
        with patch("variational_grid.cl_bz_scalper.read_history", side_effect=GridError("private/session.json SECRET")):
            status, _, raw = self.request("/api/cl-bz-history?range=24h")
            self.assertEqual(status, 503)
            self.assertNotIn(b"SECRET", raw)
            self.assertNotIn(b"session.json", raw)
            self.assertTrue(self.json("/api/cl-bz-snapshot")["details_available"])
            self.assertIsNotNone(self.json("/api/qqq-snapshot")["summary"])


if __name__ == "__main__":
    unittest.main()
