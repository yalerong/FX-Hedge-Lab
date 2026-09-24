"""HTTP 层测试。

逻辑层测得很细，接口层原来一条没测——路由、参数校验、错误码、并发写
全靠肉眼。这里起一个真的 FxRiskServer，用真的 HTTP 请求打它，
数据目录指到临时目录，不碰工作区里的真实状态。
"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import web_app


def request(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)

        # 把模块级路径指到临时目录，避免测试写坏真实工作区
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

        # 测试不许出网：汇率固定
        web_app.load_rates = lambda config, force=False: {
            "source": "test",
            "status": "test",
            "fetched_at": "2026-05-12T00:00:00Z",
            "pair_rates": {"USD": 7.2, "EUR": 7.8},
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
        status, _ = request("POST", f"{self.base}/api/reset-demo", {})
        self.assertEqual(status, 200)

    # ---------- 读 ----------

    def test_state_endpoint_returns_dashboard(self):
        status, data = request("GET", f"{self.base}/api/state")
        self.assertEqual(status, 200)
        for key in ("net_exposures", "suggestions", "portfolio", "scenario_totals", "backtest", "audit"):
            self.assertIn(key, data)

    def test_index_is_served(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])

    def test_unknown_path_is_404(self):
        status, _ = request("GET", f"{self.base}/api/nope")
        self.assertEqual(status, 404)

    # ---------- 写 ----------

    def test_create_exposure_then_delete_it(self):
        status, _ = request("POST", f"{self.base}/api/exposures", {
            "due_date": "2026-09-30",
            "currency": "USD",
            "amount": 1000,
            "direction": "receipt",
            "category": "cash_flow",
        })
        self.assertEqual(status, 200)

        _, data = request("GET", f"{self.base}/api/state")
        created = [row for row in data["exposures"] if row["due_date"] == "2026-09-30"]
        self.assertEqual(len(created), 1)
        record_id = created[0]["id"]

        status, payload = request("DELETE", f"{self.base}/api/exposures/{record_id}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["deleted"], 1)

    def test_update_exposure_with_put_keeps_id_and_audits_change(self):
        status, _ = request("POST", f"{self.base}/api/exposures", {
            "due_date": "2026-09-30",
            "currency": "USD",
            "amount": 1000,
            "direction": "receipt",
            "category": "cash_flow",
        })
        self.assertEqual(status, 200)
        _, data = request("GET", f"{self.base}/api/state")
        record_id = next(row["id"] for row in data["exposures"] if row["due_date"] == "2026-09-30")

        status, payload = request("PUT", f"{self.base}/api/exposures/{record_id}", {
            "due_date": "2026-10-31",
            "currency": "EUR",
            "amount": 2500,
            "direction": "payment",
            "category": "balance_sheet",
            "description": "changed",
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload["record"]["id"], record_id)
        self.assertEqual(payload["record"]["currency"], "EUR")
        _, data = request("GET", f"{self.base}/api/state")
        updated = next(row for row in data["exposures"] if row["id"] == record_id)
        self.assertEqual(updated["amount"], 2500)
        audit = next(row for row in data["audit"] if row["action"] == "update")
        self.assertEqual(audit["before"]["amount"], 1000)
        self.assertEqual(audit["after"]["amount"], 2500)

    def test_delete_unknown_id_is_404(self):
        status, payload = request("DELETE", f"{self.base}/api/exposures/does-not-exist")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_invalid_payloads_are_rejected_with_400(self):
        cases = [
            ("缺字段", {"currency": "USD", "amount": 100, "direction": "receipt"}),
            ("方向非法", {"due_date": "2026-09-30", "currency": "USD", "amount": 100, "direction": "sideways"}),
            ("金额为负", {"due_date": "2026-09-30", "currency": "USD", "amount": -5, "direction": "receipt"}),
            ("类目表外", {"due_date": "2026-09-30", "currency": "USD", "amount": 100,
                          "direction": "receipt", "category": "export_order"}),
            ("概率越界", {"due_date": "2026-09-30", "currency": "USD", "amount": 100,
                          "direction": "receipt", "probability": 1.5}),
            ("概率为零", {"due_date": "2026-09-30", "currency": "USD", "amount": 100,
                          "direction": "receipt", "probability": 0}),
        ]
        for name, payload in cases:
            with self.subTest(name):
                status, body = request("POST", f"{self.base}/api/exposures", payload)
                self.assertEqual(status, 400, f"{name} 应该被拒")
                self.assertIn("error", body)

    def test_bad_json_is_400_not_500(self):
        req = urllib.request.Request(
            f"{self.base}/api/exposures", data=b"{not json", method="POST"
        )
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                self.fail(f"应该 400，实际 {response.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_oversized_request_is_rejected_before_reading_the_body(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10,
        )
        try:
            connection.putrequest("POST", "/api/exposures")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(web_app.MAX_REQUEST_BODY_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

        self.assertEqual(response.status, 400)
        self.assertIn("5 MB", body["error"])

    def test_rejected_request_does_not_wait_for_declared_body(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=2,
        )
        try:
            connection.putrequest("POST", "/api/exposures")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "1024")
            connection.putheader("Origin", "https://evil.example")
            connection.endheaders()
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

        self.assertEqual(response.status, 403)
        self.assertIn("origin", body["error"].lower())

    def test_config_update_is_merged_not_replaced(self):
        status, payload = request("POST", f"{self.base}/api/config", {"default_hedge_ratio": 0.6})
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["default_hedge_ratio"], 0.6)
        # 只传了一个字段，其他默认值不能被抹掉
        self.assertEqual(payload["config"]["base_currency"], "CNY")
        self.assertIn("supported_currencies", payload["config"])

    def test_export_import_and_backup_restore_round_trip_workspace(self):
        _, original = request("GET", f"{self.base}/api/state")
        status, exported = request("GET", f"{self.base}/api/export")
        self.assertEqual(status, 200)
        self.assertIn("state", exported)
        self.assertEqual(exported["state"]["exposures"], original["exposures"])

        request("POST", f"{self.base}/api/exposures", {
            "due_date": "2027-01-31", "currency": "USD", "amount": 321,
            "direction": "receipt", "category": "cash_flow",
        })
        status, restored = request("POST", f"{self.base}/api/import", {"state": exported["state"]})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(restored["backup_count"], 1)
        _, after_import = request("GET", f"{self.base}/api/state")
        self.assertEqual(after_import["exposures"], original["exposures"])

        request("POST", f"{self.base}/api/exposures", {
            "due_date": "2027-02-28", "currency": "USD", "amount": 654,
            "direction": "receipt", "category": "cash_flow",
        })
        status, backups = request("GET", f"{self.base}/api/backups")
        self.assertEqual(status, 200)
        self.assertTrue(backups["backups"])
        status, _ = request(
            "POST",
            f"{self.base}/api/backups/{backups['backups'][0]['name']}/restore",
            {},
        )
        self.assertEqual(status, 200)

    def test_exported_workspace_over_old_limit_can_be_imported(self):
        state = web_app.sample_state()
        state["plans"] = [{
            "id": "large-plan",
            "label": "large export round trip",
            "created_at": web_app.now_iso(),
            "config": {},
            "rate_snapshot": {"status": "live", "pair_rates": {"USD": 7.2}},
            "rows": [{
                "currency": "USD",
                "period": "2026-12",
                "action": "sell_foreign",
                "forecast_reason": "x" * (6 * 1024 * 1024),
            }],
        }]
        web_app.write_json(web_app.STATE_FILE, state)

        status, exported = request("GET", f"{self.base}/api/workspace/export")
        self.assertEqual(status, 200)
        self.assertGreater(len(json.dumps(exported).encode("utf-8")), 5 * 1024 * 1024)

        status, imported = request("POST", f"{self.base}/api/workspace/import", exported)
        self.assertEqual(status, 200)
        self.assertTrue(imported["ok"])

    def test_mutating_api_rejects_cross_origin_and_non_json_requests(self):
        status, body = request("POST", f"{self.base}/api/exposures", {
            "due_date": "2026-09-30", "currency": "USD", "amount": 100,
            "direction": "receipt", "category": "cash_flow",
        }, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("origin", body["error"].lower())

        req = urllib.request.Request(
            f"{self.base}/api/exposures", data=b"{}", method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                self.fail(f"expected 415, got {response.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 415)

    # ---------- 审计 ----------

    def test_every_mutation_lands_in_the_audit_log(self):
        request("POST", f"{self.base}/api/exposures", {
            "due_date": "2026-10-31", "currency": "USD", "amount": 2000,
            "direction": "payment", "category": "balance_sheet",
        })
        _, data = request("GET", f"{self.base}/api/state")
        record_id = next(row["id"] for row in data["exposures"] if row["due_date"] == "2026-10-31")
        request("DELETE", f"{self.base}/api/exposures/{record_id}")
        request("POST", f"{self.base}/api/config", {"risk_limit_cny": 12345})

        _, data = request("GET", f"{self.base}/api/state")
        audit = data["audit"]
        actions = [(row["action"], row["collection"]) for row in audit]
        self.assertIn(("create", "exposures"), actions)
        self.assertIn(("delete", "exposures"), actions)
        self.assertIn(("update", "config"), actions)

        # 日志是倒序的，最新一条在最前，且删除记录里留着删掉之前的样子
        deleted = next(row for row in audit if row["action"] == "delete")
        self.assertEqual(deleted["before"]["amount"], 2000)
        self.assertIsNone(deleted["after"])

        config_change = next(row for row in audit if row["collection"] == "config")
        self.assertEqual(config_change["after"]["risk_limit_cny"]["to"], 12345)

    def test_one_oversized_entry_does_not_blank_the_whole_log(self):
        """尾部窗口读不到完整记录时要退回整份读。

        reset 那种条目会把整个工作区连同全部方案快照塞进 before/after，
        单条就可能超过 256KB 的尾部窗口。那时尾部里一条完整记录都没有，
        直接返回空等于「变更记录」整块凭空消失。
        """
        request("POST", f"{self.base}/api/exposures", {
            "due_date": "2026-08-31", "currency": "USD", "amount": 123,
            "direction": "receipt", "category": "cash_flow",
        })
        _, before = request("GET", f"{self.base}/api/state")
        self.assertTrue(before["audit"], "前置条件：日志里应该已经有记录")

        # 追加一条超过尾部窗口的巨型记录
        giant = {"at": "2026-08-31T00:00:00Z", "action": "reset", "collection": "workspace",
                 "id": None, "before": {"blob": "x" * (web_app.AUDIT_TAIL_BYTES + 5000)},
                 "after": None}
        with web_app.AUDIT_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(giant) + chr(10))

        _, after = request("GET", f"{self.base}/api/state")
        self.assertTrue(after["audit"], "一条超大记录不该把整段历史读没了")
        self.assertEqual(after["audit"][0]["action"], "reset")

    def test_audit_recovers_large_entry_that_straddles_tail_window(self):
        giant = {"at": "2026-08-31T00:00:00Z", "action": "reset", "collection": "workspace",
                 "id": None, "before": {"blob": "x" * (web_app.AUDIT_TAIL_BYTES + 5000)},
                 "after": None}
        small = {"at": "2026-08-31T00:01:00Z", "action": "create", "collection": "exposures",
                 "id": "small", "before": None, "after": {"id": "small"}}
        web_app.AUDIT_LOG_FILE.write_text(
            json.dumps(giant) + chr(10) + json.dumps(small) + chr(10),
            encoding="utf-8",
        )

        rows = web_app.read_audit(30)

        actions = [row["action"] for row in rows]
        self.assertEqual(actions[0], "create")
        self.assertIn("reset", actions)

    def test_failed_validation_leaves_no_audit_entry(self):
        _, before = request("GET", f"{self.base}/api/state")
        request("POST", f"{self.base}/api/exposures", {"currency": "USD"})
        _, after = request("GET", f"{self.base}/api/state")
        self.assertEqual(len(after["audit"]), len(before["audit"]))

    # ---------- 并发 ----------

    def test_concurrent_writes_do_not_lose_records(self):
        # 状态是"整份读出来改完再整份写回"，没有锁就会互相覆盖。
        count = 12
        errors: list[Exception] = []

        def post(index: int) -> None:
            try:
                status, _ = request("POST", f"{self.base}/api/exposures", {
                    "due_date": "2026-11-30",
                    "currency": "USD",
                    "amount": 100 + index,
                    "direction": "receipt",
                    "category": "cash_flow",
                })
                if status != 200:
                    errors.append(RuntimeError(f"status={status}"))
            except Exception as exc:  # pragma: no cover - 出错就让断言暴露
                errors.append(exc)

        threads = [threading.Thread(target=post, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(errors, [])
        _, data = request("GET", f"{self.base}/api/state")
        created = [row for row in data["exposures"] if row["due_date"] == "2026-11-30"]
        self.assertEqual(len(created), count, "并发写丢了记录")
        self.assertEqual(len({row["id"] for row in created}), count, "id 撞了")


if __name__ == "__main__":
    unittest.main()
