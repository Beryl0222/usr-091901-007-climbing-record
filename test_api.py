"""HTTP 接口端到端测试：三类读模型分离、命令写入与错误码。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from service import Handler


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个用例换一个干净的领域实例，但复用同一个服务器。
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        service.service = service.RecordService()
        self.seq = 0

    def call(self, path, payload=None, method="POST"):
        self.seq += 1
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        with urlopen(req, timeout=2) as response:
            return response.status, json.load(response)

    def get_json(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return response.status, json.load(response)

    def expect_error(self, path, payload, status):
        with self.assertRaises(HTTPError) as cm:
            self.call(path, payload)
        self.assertEqual(cm.exception.code, status)
        body = json.loads(cm.exception.read())
        cm.exception.close()
        return body

    def call_ignore_conflict(self, path, payload):
        try:
            self.call(path, payload)
        except HTTPError as exc:
            if exc.code != 409:
                raise
            exc.read(); exc.close()

    def seed_guiyang_final(self, *, proposal="rec-958", finish="9.58",
                           backup="9.5802", ratify=True, close_window=True,
                           race="R-F1", attempt="A1", team="T1",
                           climbers=("甲", "乙")):
        t = self.seq * 0  # 时间戳用固定值，与领域测试保持同一时间线
        self.call_ignore_conflict("/rounds/final", {"at": 1000})
        self.call("/rounds/final/races", {"race_id": race, "at": 1001, "lane": 4})
        self.call("/teams", {"team_id": team, "at": 1002, "name": "贵阳队"})
        self.call(f"/races/{race}/roster",
                  {"team_id": team, "at": 1003,
                   "climber_order": list(climbers)})
        self.call_ignore_conflict("/calibrations", {"cert_id": "CERT-P",
                                    "device_id": "TP",
                                    "valid_from": 0, "at": 900, "channel": "primary"})
        self.call_ignore_conflict("/calibrations", {"cert_id": "CERT-B",
                                    "device_id": "TB",
                                    "valid_from": 0, "at": 900, "channel": "backup"})
        self.call_ignore_conflict("/rules", {"version": "v1", "at": 500,
                             "effective_at": 0, "record_threshold": "9.58",
                             "precision": 2, "expected_splits": 1})
        self.call(f"/races/{race}/attempts",
                  {"attempt_id": attempt, "team_id": team, "at": 2000})
        self.call(f"/attempts/{attempt}/readings",
                  {"source": "referee-terminal", "reading_id": "rx",
                   "reading_type": "reaction", "value": "0.145",
                   "occurred_at": 2010})
        for channel, device, rid, at in (
                ("primary", "TP", "sg-p", 2010), ("backup", "TB", "sg-b", 2010)):
            self.call(f"/attempts/{attempt}/readings",
                      {"source": f"timer-{channel}", "reading_id": rid,
                       "reading_type": "start-gate", "value": "0.0",
                       "occurred_at": at, "device_id": device})
        self.call(f"/attempts/{attempt}/readings",
                  {"source": "timer-backup", "reading_id": "sp-b",
                   "reading_type": "split", "value": "5.22", "occurred_at": 2011,
                   "device_id": "TB", "segment": "leg1"})
        self.call(f"/attempts/{attempt}/readings",
                  {"source": "timer-primary", "reading_id": "sp-p",
                   "reading_type": "split", "value": "5.21", "occurred_at": 2011,
                   "device_id": "TP", "segment": "leg1"})
        self.call(f"/attempts/{attempt}/readings",
                  {"source": "timer-backup", "reading_id": "fin-b",
                   "reading_type": "finish", "value": backup,
                   "occurred_at": 2018, "device_id": "TB"})
        self.call(f"/attempts/{attempt}/readings",
                  {"source": "timer-primary", "reading_id": "fin-p",
                   "reading_type": "finish", "value": finish,
                   "occurred_at": 2019, "device_id": "TP"})
        self.call(f"/attempts/{attempt}/confirmations",
                  {"kind": "start", "at": 2030, "judge_id": "J1"})
        self.call(f"/attempts/{attempt}/confirmations",
                  {"kind": "handoff-order", "at": 2031, "judge_id": "J1",
                   "detail": {"climber_order": list(climbers)}})
        self.call(f"/attempts/{attempt}/confirmations",
                  {"kind": "result", "at": 2032, "judge_id": "J1",
                   "note": "成绩有效"})
        if close_window:
            self.call(f"/races/{race}/appeal-window/close", {"at": 2100})
        self.call(f"/attempts/{attempt}/record-proposal",
                  {"at": 2200, "proposal_id": proposal})
        if ratify:
            self.call(f"/records/{proposal}/ratify",
                      {"at": 2300, "technical_delegate": "TD-贵阳"})
        return proposal

    # ------------------------------------------------------------- 基本身份

    def test_health_and_contract(self):
        _, health = self.get_json("/health")
        self.assertEqual(health["service"], "climbing-record")
        _, contract = self.get_json("/contract")
        self.assertEqual(contract["contract_version"], "1.1")
        self.assertEqual([r["id"] for r in contract["rounds"]],
                         ["heat", "quarterfinal", "semifinal", "final"])

    # ----------------------------------------------------------- 三类读模型

    def test_three_read_models_are_separate(self):
        pid = self.seed_guiyang_final()
        _, instant = self.get_json("/attempts/A1/instant")
        _, official = self.get_json("/races/R-F1/result")
        _, record = self.get_json(f"/records/{pid}")
        self.assertEqual(instant["model"], "instant")
        self.assertEqual(official["model"], "official")
        self.assertEqual(record["model"], "record")
        # 即时成绩不带正式排名；正式赛果带排名但不带校准证书；纪录档案两者皆可溯。
        self.assertNotIn("rankings", instant)
        self.assertNotIn("calibration_certificates", official)
        self.assertEqual({c["cert_id"] for c in record["calibration_certificates"]},
                         {"CERT-P", "CERT-B"})
        self.assertEqual(official["rankings"][0]["attempt_id"], "A1")
        self.assertEqual(instant["state"], "纪录已核准")

    def test_record_dossier_from_9_58(self):
        # 技术代表从 9.58 这一条记录直接查到四类证据与申诉截止状态。
        pid = self.seed_guiyang_final(finish="9.58")
        _, record = self.get_json(f"/records/{pid}")
        self.assertEqual(record["finish_time"], 9.58)
        self.assertEqual(record["comparison"], "tied")
        self.assertTrue(record["calibration_certificates"])
        self.assertTrue(record["raw_readings"])
        self.assertEqual({c["kind"] for c in record["judge_confirmations"]},
                         {"start", "handoff-order", "result"})
        self.assertEqual(record["appeals"]["window_closed_at"], 2100)
        self.assertEqual(record["technical_delegate"], "TD-贵阳")

    def test_tie_then_faster_record_over_http(self):
        self.seed_guiyang_final(proposal="rec-958", finish="9.58",
                                race="R-F1", attempt="A1")
        # 更快的第二场
        self.seed_guiyang_final(proposal="rec-956", finish="9.56", backup="9.5598",
                                race="R-F2", attempt="A2", team="T2",
                                climbers=("客1", "客2"))
        _, listing = self.get_json("/records?status=ratified")
        ids = {r["record_id"] for r in listing["records"]}
        self.assertEqual(ids, {"rec-958", "rec-956"})

    # -------------------------------------------------------------- 写入语义

    def test_retransmission_is_idempotent_over_http(self):
        self.seed_guiyang_final(ratify=False)
        _, before = self.get_json("/events")
        n_before = len(before["events"])
        payload = {"source": "timer-primary", "reading_id": "fin-p",
                   "reading_type": "finish", "value": "9.58",
                   "occurred_at": 2019, "device_id": "TP"}
        status, body = self.call("/attempts/A1/readings", payload)
        self.assertEqual(status, 201)
        self.assertTrue(body["result"]["duplicate"])
        _, after = self.get_json("/events")
        self.assertEqual(len(after["events"]), n_before)

    def test_commands_endpoint_matches_semantic_route(self):
        self.call("/commands", {"command": "register-round", "round_id": "heat",
                                "at": 1000})
        _, contract = self.get_json("/contract")
        # /commands 与别名路由落到同一领域方法。
        self.call("/rounds/quarterfinal", {"at": 1000})
        _, events = self.get_json("/events")
        rounds = [e["data"]["round_id"] for e in events["events"]
                  if e["event"] == "round-registered"]
        self.assertEqual(rounds, ["heat", "quarterfinal"])

    # --------------------------------------------------------------- 错误码

    def test_error_codes(self):
        # 404：未知资源
        with self.assertRaises(HTTPError) as cm:
            self.get_json("/attempts/nope/instant")
        self.assertEqual(cm.exception.code, 404); cm.exception.close()
        # 400：缺字段 / 非法值
        body = self.expect_error("/rounds/final", {}, 400)  # 缺 at
        self.assertEqual(body["code"], "invalid")
        self.call("/rounds/final", {"at": 1})
        # 409：重复登记
        self.expect_error("/rounds/final", {"at": 2}, 409)
        # 400：非法 JSON
        req = Request(f"{self.base_url}/rounds/semifinal",
                      data=b"{not-json", headers={"Content-Type": "application/json"},
                      method="POST")
        with self.assertRaises(HTTPError) as cm:
            urlopen(req, timeout=2)
        self.assertEqual(cm.exception.code, 400); cm.exception.close()

    def test_ratification_blockers_surfaces_reasons(self):
        # 有读数但没有校准/确认/关窗：核准返回 409 且 reasons 非空。
        self.call("/rounds/final", {"at": 1000})
        self.call("/rounds/final/races", {"race_id": "R1", "at": 1001})
        self.call("/teams", {"team_id": "T1", "at": 1002, "name": "队"})
        self.call("/races/R1/roster", {"team_id": "T1", "at": 1003,
                                       "climber_order": ["甲", "乙"]})
        self.call("/rules", {"version": "v1", "at": 400, "effective_at": 0,
                             "record_threshold": "9.58", "precision": 2})
        self.call("/races/R1/attempts", {"attempt_id": "A1", "team_id": "T1",
                                         "at": 2000})
        self.call("/attempts/A1/readings",
                  {"source": "timer-primary", "reading_id": "f1",
                   "reading_type": "finish", "value": "9.58",
                   "occurred_at": 2019, "device_id": "TP"})
        self.call("/attempts/A1/record-proposal", {"at": 2200,
                                                   "proposal_id": "rec-x"})
        body = self.expect_error("/records/rec-x/ratify",
                                 {"at": 2300, "technical_delegate": "TD"}, 409)
        self.assertTrue(body["reasons"])
        _, record = self.get_json("/records/rec-x")
        self.assertEqual(record["status"], "pending-verification")


if __name__ == "__main__":
    unittest.main()
