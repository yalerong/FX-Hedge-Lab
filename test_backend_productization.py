from __future__ import annotations

import copy
import csv
import base64
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

import web_app


def http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}, dict(response.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return exc.code, parsed, dict(exc.headers)


def http_text(method: str, url: str, payload: dict | None = None) -> tuple[int, str, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read().decode("utf-8-sig"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def http_bytes(method: str, url: str, payload: dict | None = None) -> tuple[int, bytes, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class BackendProductizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls._saved = {
            "DATA_DIR": web_app.DATA_DIR,
            "STATE_FILE": web_app.STATE_FILE,
            "RATES_CACHE_FILE": web_app.RATES_CACHE_FILE,
            "AUDIT_LOG_FILE": web_app.AUDIT_LOG_FILE,
            "load_rates": web_app.load_rates,
        }
        web_app.DATA_DIR = tmp
        web_app.STATE_FILE = tmp / "fx_workspace.json"
        web_app.RATES_CACHE_FILE = tmp / "rates_cache.json"
        web_app.AUDIT_LOG_FILE = tmp / "audit_log.jsonl"
        web_app.load_rates = lambda config, force=False: {
            "source": "test",
            "status": "test",
            "fetched_at": "2026-05-12T00:00:00Z",
            "pair_rates": {"USD": 7.2},
        }
        cls.server = web_app.FxRiskServer(("127.0.0.1", 0), web_app.FxRiskHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        for key, value in cls._saved.items():
            setattr(web_app, key, value)
        cls._tmp.cleanup()

    def setUp(self):
        status, _, _ = http_json("POST", f"{self.base}/api/workspace/sample", {})
        self.assertEqual(status, 200)

    def test_missing_rate_blocks_advice_and_forward_fill(self):
        state = copy.deepcopy(web_app.DEMO_STATE)
        state["config"] = {**web_app.DEFAULT_CONFIG, "supported_currencies": ["USD", "CHF"]}
        state["exposures"] = [{
            "id": "missing-rate",
            "due_date": "2027-03-31",
            "currency": "CHF",
            "amount": 100000,
            "direction": "receipt",
            "category": "cash_flow",
            "probability": 1,
        }]
        state["hedges"] = []
        state["settlements"] = []
        dashboard = web_app.build_dashboard(
            state,
            {"source": "test", "status": "test", "fetched_at": "x", "pair_rates": {"USD": 7.2}},
            forecast_doc={},
        )

        self.assertEqual(dashboard["suggestions"], [])
        chf = next(row for row in dashboard["net_exposures"] if row["currency"] == "CHF")
        self.assertFalse(chf["rate_available"])
        self.assertIsNone(chf["current_rate"])
        self.assertIsNone(chf["cny_risk"])
        self.assertTrue(dashboard["portfolio"]["rate_missing"])

    def test_first_run_creates_empty_workspace_with_metadata(self):
        web_app.STATE_FILE.unlink(missing_ok=True)
        state = web_app.ensure_state()

        self.assertEqual(state["exposures"], [])
        self.assertEqual(state["hedges"], [])
        self.assertEqual(state["settlements"], [])
        self.assertEqual(state["metadata"]["data_mode"], "empty")
        self.assertFalse(state["metadata"]["setup_complete"])

    def test_state_file_recovers_from_backup_when_main_json_is_corrupt(self):
        original = web_app.empty_state()
        original["exposures"].append({
            "id": "safe",
            "due_date": "2027-01-31",
            "currency": "USD",
            "amount": 123,
            "direction": "receipt",
            "category": "cash_flow",
            "probability": 1,
        })
        web_app.save_state(original)
        web_app.STATE_FILE.write_text("{broken", encoding="utf-8")

        recovered = web_app.ensure_state()

        self.assertEqual(recovered["exposures"][0]["id"], "safe")
        self.assertEqual(json.loads(web_app.STATE_FILE.read_text(encoding="utf-8"))["exposures"][0]["id"], "safe")

    def test_mutating_requests_require_json_and_same_origin(self):
        req = urllib.request.Request(
            f"{self.base}/api/exposures",
            data=json.dumps({"currency": "USD"}).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "text/plain")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 415)

        status, body, _ = http_json(
            "POST",
            f"{self.base}/api/exposures",
            {"currency": "USD"},
            {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)
        self.assertIn("error", body)

    def test_security_headers_are_returned_on_api_and_static_paths(self):
        api_status, _, api_headers = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(api_status, 200)
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as response:
            static_headers = dict(response.headers)

        for headers in (api_headers, static_headers):
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(headers.get("X-Frame-Options"), "DENY")
            self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""))

    def test_web_static_paths_cannot_escape_web_root(self):
        secret = web_app.DATA_DIR / "forecast_signals.json"
        secret.write_text('{"secret": "not static"}', encoding="utf-8")

        for path in (
            "/web/../data/forecast_signals.json",
            "/web/..%5cdata%5cforecast_signals.json",
        ):
            with self.subTest(path=path):
                status, body, _ = http_text("GET", f"{self.base}{path}")
                self.assertEqual(status, 404)
                self.assertNotIn("not static", body)

    def test_cached_refresh_error_blocks_plan_freeze(self):
        saved_load_rates = web_app.load_rates
        web_app.load_rates = lambda config, force=False: {
            "source": "test",
            "status": "cached_after_refresh_error",
            "last_error": "timeout",
            "fetched_at": "2026-05-12T00:00:00Z",
            "pair_rates": {"USD": 7.2, "EUR": 7.8},
        }
        try:
            status, body, _ = http_json("POST", f"{self.base}/api/plans", {"label": "unsafe"})
        finally:
            web_app.load_rates = saved_load_rates

        self.assertEqual(status, 400)
        self.assertIn("汇率", body["error"])

    def test_non_loopback_bind_requires_explicit_allow_network(self):
        self.assertTrue(web_app.is_loopback_host("127.0.0.2"))
        self.assertTrue(web_app.is_loopback_host("[::1]"))
        with self.assertRaises(ValueError):
            web_app.require_allowed_bind("0.0.0.0")
        web_app.require_allowed_bind("0.0.0.0", allow_network=True)

    def test_validation_rejects_bad_currency_dates_and_ranges(self):
        cases = [
            ("/api/exposures", {"due_date": "2027-01-31", "currency": "usd", "amount": 1, "direction": "receipt"}),
            ("/api/exposures", {"due_date": "bad-date", "currency": "USD", "amount": 1, "direction": "receipt"}),
            ("/api/exposures", {"due_date": "2027-01-31", "currency": "CHF", "amount": 1, "direction": "receipt"}),
            ("/api/hedges", {"trade_date": "2027-02-01", "due_date": "2027-01-31", "currency": "USD", "amount": 1, "action": "sell_foreign", "locked_rate": 7.2}),
            ("/api/config", {"rate_cache_hours": 0}),
            ("/api/config", {"default_hedge_ratio": 1.5}),
        ]
        for path, payload in cases:
            with self.subTest(path=path, payload=payload):
                status, body, _ = http_json("POST", f"{self.base}{path}", payload)
                self.assertEqual(status, 400)
                self.assertIn("error", body)

    def test_put_updates_existing_business_rows(self):
        status, _, _ = http_json("POST", f"{self.base}/api/workspace/empty", {})
        self.assertEqual(status, 200)
        status, _, _ = http_json("POST", f"{self.base}/api/exposures", {
            "due_date": "2027-01-31",
            "currency": "USD",
            "amount": 100,
            "direction": "receipt",
            "category": "cash_flow",
        })
        self.assertEqual(status, 200)
        _, data, _ = http_json("GET", f"{self.base}/api/state")
        record_id = data["exposures"][0]["id"]

        status, body, _ = http_json("PUT", f"{self.base}/api/exposures/{record_id}", {
            "due_date": "2027-02-28",
            "currency": "USD",
            "amount": 250,
            "direction": "payment",
            "category": "order_contract",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["record"]["amount"], 250.0)
        _, after, _ = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(after["exposures"][0]["due_date"], "2027-02-28")
        self.assertEqual(after["audit"][0]["action"], "update")

    def test_workspace_export_import_validates_and_audits(self):
        status, exported, _ = http_json("GET", f"{self.base}/api/workspace/export")
        self.assertEqual(status, 200)
        self.assertIn("workspace", exported)
        imported = copy.deepcopy(exported["workspace"])
        imported["metadata"]["data_mode"] = "imported"
        imported["exposures"] = []

        status, body, _ = http_json("POST", f"{self.base}/api/workspace/import", {"workspace": imported})

        self.assertEqual(status, 200)
        self.assertEqual(body["metadata"]["data_mode"], "imported")
        _, after, _ = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(after["exposures"], [])
        self.assertEqual(after["audit"][0]["action"], "import")

        status, bad, _ = http_json("POST", f"{self.base}/api/workspace/import", {"workspace": {"exposures": "bad"}})
        self.assertEqual(status, 400)
        self.assertIn("error", bad)

    def test_state_reports_workspace_mode_setup_and_data_file(self):
        status, data, _ = http_json("GET", f"{self.base}/api/state")

        self.assertEqual(status, 200)
        self.assertEqual(data["workspace"]["data_mode"], "sample")
        self.assertTrue(data["workspace"]["setup_complete"])
        self.assertTrue(data["workspace"]["data_file"].endswith("fx_workspace.json"))

    def test_freeze_plan_rejects_when_any_exposure_rate_is_missing(self):
        status, _, _ = http_json("POST", f"{self.base}/api/config", {"supported_currencies": ["USD", "EUR", "CHF"]})
        self.assertEqual(status, 200)
        status, _, _ = http_json("POST", f"{self.base}/api/exposures", {
            "due_date": "2027-04-30",
            "currency": "CHF",
            "amount": 1000,
            "direction": "receipt",
            "category": "cash_flow",
        })
        self.assertEqual(status, 200)

        status, body, _ = http_json("POST", f"{self.base}/api/plans", {"label": "missing rate"})

        self.assertEqual(status, 400)
        self.assertIn("汇率", body["error"])

    def test_config_replaces_submitted_json_maps(self):
        status, body, _ = http_json("POST", f"{self.base}/api/config", {
            "interest_rates": {"USD": 0.05},
            "scenario_shifts": {"USD": {"optimistic": 0.02}},
            "forward_overrides": {"2027-03:USD": 7.08},
        })
        self.assertEqual(status, 200)
        self.assertNotIn("EUR", body["config"]["interest_rates"])

        status, body, _ = http_json("POST", f"{self.base}/api/config", {
            "interest_rates": {"EUR": 0.03},
            "scenario_shifts": {"EUR": {"pessimistic": -0.02}},
            "forward_overrides": {},
        })

        self.assertEqual(status, 200)
        self.assertNotIn("USD", body["config"]["interest_rates"])
        self.assertEqual(body["config"]["interest_rates"]["EUR"], 0.03)
        self.assertNotIn("USD", body["config"]["scenario_shifts"])
        self.assertEqual(body["config"]["forward_overrides"], {})

    def test_csv_import_regenerates_exported_ids_before_append(self):
        status, csv_text, _ = http_text("GET", f"{self.base}/api/csv/export?collection=exposures")
        self.assertEqual(status, 200)

        status, body, _ = http_json("POST", f"{self.base}/api/csv/import", {
            "collection": "exposures",
            "csv": csv_text,
        })
        self.assertEqual(status, 200)
        self.assertGreater(body["imported"], 0)

        status, data, _ = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(status, 200)
        ids = [row["id"] for row in data["exposures"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_workspace_validation_rejects_malformed_plan_snapshots(self):
        malformed = [
            "not a plan",
            {"config": []},
            {"rate_snapshot": []},
            {"rate_snapshot": {"pair_rates": []}},
            {"rate_snapshot": {"pair_rates": {"USD": "bad"}}},
            {"rows": "not rows"},
            {"rows": ["not a row"]},
            {"rows": [{"currency": []}]},
        ]
        for plan in malformed:
            with self.subTest(plan=plan):
                state = web_app.empty_state(setup_complete=True)
                state["plans"] = [plan]
                with self.assertRaises(ValueError):
                    web_app.validate_workspace_state(state)

    def test_workspace_validation_rejects_unsafe_plan_field_types(self):
        clean_row = {
            "period": "2027-03", "currency": "USD", "action": "sell_foreign",
            "target_hedge_ratio": 0.5, "forecast_multiplier": 1.0,
            "recommended_amount": 100.0, "trade_rate": 7.123456, "forecast_reason": None,
        }
        clean = {"id": "p1", "label": "ok", "created_at": "2026-01-01T00:00:00Z",
                 "rate_snapshot": {"status": "test", "pair_rates": {"USD": 7.2}}, "rows": [clean_row]}
        web_app.validate_plan_snapshot(clean)

        malformed = [
            {"label": ["<img src=x onerror=alert(1)>"]},
            {"created_at": 123},
            {"rate_snapshot": {"status": {"x": 1}}},
            {"rows": [{**clean_row, "trade_rate": "<img src=x onerror=alert(1)>"}]},
            {"rows": [{**clean_row, "recommended_amount": "100"}]},
            {"rows": [{**clean_row, "forecast_multiplier": True}]},
            {"rows": [{**clean_row, "target_hedge_ratio": float("nan")}]},
            {"rows": [{**clean_row, "period": ["2027-03"]}]},
        ]
        for plan in malformed:
            with self.subTest(plan=plan):
                with self.assertRaises(ValueError):
                    web_app.validate_plan_snapshot(plan)

        state = web_app.empty_state(setup_complete=True)
        state["plans"] = [{**clean, "rows": [{**clean_row, "trade_rate": "<img src=x onerror=alert(1)>"}]}]
        status, body, _ = http_json("POST", f"{self.base}/api/workspace/import", {"workspace": state})
        self.assertEqual(status, 400)
        self.assertIn("trade_rate", body["error"])

    def test_workspace_import_rejects_non_object_metadata(self):
        for metadata in (None, [], "sample"):
            with self.subTest(metadata=metadata):
                state = web_app.empty_state(setup_complete=True)
                state["metadata"] = metadata
                status, body, _ = http_json("POST", f"{self.base}/api/workspace/import", {"workspace": state})
                self.assertEqual(status, 400)
                self.assertIn("metadata", body["error"])

    def test_workspace_import_requires_cny_base_currency(self):
        for base_currency in ("USD", {"code": "CNY"}, ["CNY"]):
            with self.subTest(base_currency=base_currency):
                state = web_app.empty_state(setup_complete=True)
                state["config"]["base_currency"] = base_currency
                status, body, _ = http_json("POST", f"{self.base}/api/workspace/import", {"workspace": state})
                self.assertEqual(status, 400)
                self.assertIn("base_currency", body["error"])
        status, data, _ = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["base_currency"], "CNY")

    def test_rejected_mutation_does_not_rotate_backups(self):
        before = [row["name"] for row in web_app.list_backups()]
        self.assertTrue(before)

        # 端点级校验放过（合法的币种列表），整体校验才发现样例记录仍在用 USD
        status, body, _ = http_json("POST", f"{self.base}/api/config", {"supported_currencies": ["JPY"]})

        self.assertEqual(status, 400)
        self.assertIn("USD", body["error"])
        self.assertEqual([row["name"] for row in web_app.list_backups()], before)
        _, data, _ = http_json("GET", f"{self.base}/api/state")
        self.assertIn("USD", data["config"]["supported_currencies"])

    def test_backup_listing_tolerates_files_deleted_mid_scan(self):
        directory = web_app.backup_dir()
        directory.mkdir(parents=True, exist_ok=True)
        doomed = directory / "20990101T000000Z-doomed-deadbeef.json"
        doomed.write_text("{}", encoding="utf-8")
        original_stat = Path.stat

        def racing_stat(self, *args, **kwargs):
            if self.name == doomed.name:
                raise FileNotFoundError(str(self))
            return original_stat(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", racing_stat):
            names = [row["name"] for row in web_app.list_backups()]
            latest = web_app.latest_backup_file()
            undo = web_app.latest_undo_backup_file()
            web_app.prune_backups()

        self.assertNotIn(doomed.name, names)
        self.assertNotEqual(latest, doomed)
        self.assertNotEqual(undo, doomed)
        doomed.unlink()

    def test_workspace_reads_preserve_updated_at_metadata(self):
        fixed = "2026-01-02T03:04:05Z"
        state = web_app.empty_state(setup_complete=True)
        state["metadata"]["updated_at"] = fixed
        web_app.STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        loaded = web_app.ensure_state()
        self.assertEqual(loaded["metadata"]["updated_at"], fixed)

        status, exported, _ = http_json("GET", f"{self.base}/api/workspace/export")
        self.assertEqual(status, 200)
        self.assertEqual(exported["metadata"]["updated_at"], fixed)

    def test_workspace_load_preserves_legacy_exposure_categories(self):
        state = web_app.empty_state(setup_complete=True)
        legacy = {
            "id": "legacy-1",
            "due_date": "2027-05-31",
            "currency": "USD",
            "amount": 1000,
            "direction": "receipt",
            "category": "export_order",
            "probability": 1,
        }
        state["exposures"] = [legacy]

        normalized = web_app.validate_workspace_state(state)

        self.assertEqual(normalized["exposures"][0]["category"], "export_order")
        with self.assertRaises(ValueError):
            web_app.validate_exposure(dict(legacy), normalized["config"])

    def test_corrupt_state_file_is_preserved_during_backup_recovery(self):
        state = web_app.empty_state()
        state["exposures"].append({
            "id": "recover-me",
            "due_date": "2027-05-31",
            "currency": "USD",
            "amount": 1000,
            "direction": "receipt",
            "category": "cash_flow",
            "probability": 1,
        })
        web_app.save_state(state)
        web_app.STATE_FILE.write_text("{broken", encoding="utf-8")

        recovered = web_app.ensure_state()
        corrupt_files = list(web_app.STATE_FILE.parent.glob("fx_workspace.json.corrupt-*"))

        self.assertEqual(recovered["exposures"][0]["id"], "recover-me")
        self.assertTrue(corrupt_files)

    def test_latest_backup_restore_has_404_without_backup_and_backs_up_current_state(self):
        for path in web_app.backup_dir().glob("*.json"):
            path.unlink()
        status, body, _ = http_json("POST", f"{self.base}/api/backups/latest/restore", {})
        self.assertEqual(status, 404)
        self.assertIn("备份", body["error"])

        status, _, _ = http_json("POST", f"{self.base}/api/exposures", {
            "due_date": "2027-06-30",
            "currency": "USD",
            "amount": 4321,
            "direction": "receipt",
            "category": "cash_flow",
        })
        self.assertEqual(status, 200)
        before = len(list(web_app.backup_dir().glob("*.json")))
        status, _, _ = http_json("POST", f"{self.base}/api/backups/latest/restore", {})
        self.assertEqual(status, 200)
        after = len(list(web_app.backup_dir().glob("*.json")))
        self.assertGreater(after, before)

    def test_latest_backup_restore_rolls_back_the_last_business_write(self):
        for path in web_app.backup_dir().glob("*.json"):
            path.unlink()
        status, _, _ = http_json("POST", f"{self.base}/api/workspace/empty", {})
        self.assertEqual(status, 200)

        status, _, _ = http_json("POST", f"{self.base}/api/exposures", {
            "due_date": "2027-06-30",
            "currency": "USD",
            "amount": 4321,
            "direction": "receipt",
            "category": "cash_flow",
        })
        self.assertEqual(status, 200)
        _, before_restore, _ = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(len(before_restore["exposures"]), 1)

        status, _, _ = http_json("POST", f"{self.base}/api/backups/latest/restore", {})

        self.assertEqual(status, 200)
        _, after_restore, _ = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(after_restore["exposures"], [])

    def test_latest_backup_restore_reverts_the_latest_change(self):
        status, _, _ = http_json("POST", f"{self.base}/api/workspace/empty", {})
        self.assertEqual(status, 200)
        for path in web_app.backup_dir().glob("*.json"):
            path.unlink()

        status, _, _ = http_json("POST", f"{self.base}/api/exposures", {
            "due_date": "2027-06-30",
            "currency": "USD",
            "amount": 4321,
            "direction": "receipt",
            "category": "cash_flow",
        })
        self.assertEqual(status, 200)

        status, _, _ = http_json("POST", f"{self.base}/api/backups/latest/restore", {})
        self.assertEqual(status, 200)
        self.assertEqual(web_app.ensure_state()["exposures"], [])

    def test_atomic_json_write_flushes_file_to_disk_before_replace(self):
        path = web_app.DATA_DIR / "durable.json"
        with mock.patch.object(web_app.os, "fsync") as fsync:
            web_app.write_json(path, {"ok": True})
        fsync.assert_called_once()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})

    def test_non_loopback_server_prints_security_warning(self):
        fake_server = mock.Mock()
        with (
            mock.patch.object(web_app, "ensure_state"),
            mock.patch.object(web_app, "FxRiskServer", return_value=fake_server),
            mock.patch("builtins.print") as printer,
        ):
            web_app.run("0.0.0.0", 8765, allow_network=True)

        messages = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("安全警告", messages)
        self.assertIn("未提供身份认证", messages)
        fake_server.serve_forever.assert_called_once_with()

    def test_advanced_config_rejects_values_that_would_corrupt_advice(self):
        invalid_patches = [
            {"interest_rates": {"USD": "not-a-number"}},
            {"forward_overrides": {"2027-03:USD": 0}},
            {"month_currency_hedge_ratios": {"2027-03": {"USD": 1.2}}},
            {"scenario_shifts": {"USD": {"optimistic": -1.01}}},
            {"monthly_average_rates": {"2027-03:USD": -7.1}},
        ]
        for patch in invalid_patches:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                web_app.validate_config(patch)

    def test_config_rejects_non_finite_numbers(self):
        for patch in ({"risk_limit_cny": float("nan")}, {"rate_cache_hours": float("inf")}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                web_app.validate_config(patch)

    def test_csv_export_escapes_formula_text(self):
        status, _, _ = http_json("POST", f"{self.base}/api/workspace/empty", {})
        self.assertEqual(status, 200)
        status, _, _ = http_json("POST", f"{self.base}/api/exposures", {
            "due_date": "2027-07-31",
            "currency": "USD",
            "amount": 100,
            "direction": "receipt",
            "category": "cash_flow",
            "description": "=SUM(1,1)",
        })
        self.assertEqual(status, 200)

        status, body, headers = http_text("GET", f"{self.base}/api/csv/export?collection=exposures")

        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        rows = list(csv.DictReader(io.StringIO(body)))
        self.assertEqual(rows[0]["description"], "'=SUM(1,1)")

    def test_csv_import_validates_entire_batch_before_saving(self):
        status, _, _ = http_json("POST", f"{self.base}/api/workspace/empty", {})
        self.assertEqual(status, 200)
        csv_body = (
            "due_date,currency,amount,direction,category,description\n"
            "2027-08-31,USD,100,receipt,cash_flow,ok\n"
            "2027-08-31,usd,200,receipt,cash_flow,bad currency\n"
        )

        status, body, _ = http_json("POST", f"{self.base}/api/csv/import", {
            "collection": "exposures",
            "csv": csv_body,
        })

        self.assertEqual(status, 400)
        self.assertIn("第 2 行", body["error"])
        _, after, _ = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(after["exposures"], [])

        status, body, _ = http_json("POST", f"{self.base}/api/csv/import", {
            "collection": "exposures",
            "csv": csv_body.replace("usd", "EUR"),
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["imported"], 2)
        _, after, _ = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(len(after["exposures"]), 2)

    def test_xlsx_export_import_round_trips_business_rows_without_new_dependencies(self):
        status, _, _ = http_json("POST", f"{self.base}/api/workspace/empty", {})
        self.assertEqual(status, 200)
        status, _, _ = http_json("POST", f"{self.base}/api/exposures", {
            "due_date": "2027-09-30",
            "currency": "USD",
            "amount": 555,
            "direction": "receipt",
            "category": "cash_flow",
            "description": "excel row",
        })
        self.assertEqual(status, 200)

        status, blob, headers = http_bytes("GET", f"{self.base}/api/xlsx/export?collection=exposures")

        self.assertEqual(status, 200)
        self.assertIn("spreadsheetml.sheet", headers["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(blob)) as book:
            self.assertIn("xl/worksheets/sheet1.xml", book.namelist())

        status, _, _ = http_json("POST", f"{self.base}/api/workspace/empty", {})
        self.assertEqual(status, 200)
        status, body, _ = http_json("POST", f"{self.base}/api/xlsx/import", {
            "collection": "exposures",
            "data_b64": base64.b64encode(blob).decode("ascii"),
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["imported"], 1)
        _, after, _ = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(after["exposures"][0]["description"], "excel row")

    def test_xlsx_import_resolves_worksheet_through_workbook_relationships(self):
        status, _, _ = http_json("POST", f"{self.base}/api/workspace/empty", {})
        self.assertEqual(status, 200)
        status, _, _ = http_json("POST", f"{self.base}/api/exposures", {
            "due_date": "2027-09-30", "currency": "USD", "amount": 12, "direction": "receipt",
            "category": "cash_flow", "description": "second sheet",
        })
        self.assertEqual(status, 200)
        status, blob, _ = http_bytes("GET", f"{self.base}/api/xlsx/export?collection=exposures")
        self.assertEqual(status, 200)

        # 模拟删过第一张表的工作簿：唯一的工作表叫 sheet2.xml，只有 rels 指向它
        renamed = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(blob)) as source, zipfile.ZipFile(renamed, "w") as target:
            for name in source.namelist():
                data = source.read(name)
                if name == "xl/worksheets/sheet1.xml":
                    name = "xl/worksheets/sheet2.xml"
                elif name in {"xl/_rels/workbook.xml.rels", "[Content_Types].xml"}:
                    data = data.replace(b"sheet1.xml", b"sheet2.xml")
                target.writestr(name, data)

        status, _, _ = http_json("POST", f"{self.base}/api/workspace/empty", {})
        self.assertEqual(status, 200)
        status, body, _ = http_json("POST", f"{self.base}/api/xlsx/import", {
            "collection": "exposures",
            "data_b64": base64.b64encode(renamed.getvalue()).decode("ascii"),
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["imported"], 1)
        _, after, _ = http_json("GET", f"{self.base}/api/state")
        self.assertEqual(after["exposures"][0]["description"], "second sheet")

        # 关系表指向一个不存在的部件，要报 400 而不是 500
        broken = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(blob)) as source, zipfile.ZipFile(broken, "w") as target:
            for name in source.namelist():
                data = source.read(name)
                if name == "xl/_rels/workbook.xml.rels":
                    data = data.replace(b"sheet1.xml", b"sheet9.xml")
                target.writestr(name, data)
        status, body, _ = http_json("POST", f"{self.base}/api/xlsx/import", {
            "collection": "exposures",
            "data_b64": base64.b64encode(broken.getvalue()).decode("ascii"),
        })
        self.assertEqual(status, 400)
        self.assertIn("sheet9.xml", body["error"])


if __name__ == "__main__":
    unittest.main()
