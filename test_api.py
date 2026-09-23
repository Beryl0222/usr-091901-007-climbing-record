"""HTTP 端到端：贵阳站 9.58 通过真实 HTTP 接口完成三级发布。"""

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from service import Handler, build_service

BASE = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)


def iso(m):
    return m.isoformat()


class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        Handler.service = build_service()

    def call(self, method, path, payload=None, query=None):
        url = f"{self.base}{path}"
        if query:
            url += "?" + urlencode(query)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_record_9_58_end_to_end(self):
        # 管理端准备
        self.call("POST", "/admin/meetings", {
            "meeting_id": "M", "name": "贵阳站", "date": "2026-09-23",
            "ts": iso(BASE - timedelta(hours=2)), "protest_window_seconds": 1800})
        self.call("POST", "/admin/precision-rules", {
            "rule_id": "P1", "channel_tolerance_seconds": 0.005,
            "false_start_threshold_seconds": 0.100, "timing_resolution": "0.01",
            "effective_from": iso(BASE - timedelta(days=30)),
            "issued_by": "tc", "ts": iso(BASE - timedelta(hours=2))})
        self.call("POST", "/admin/standards", {
            "rule_id": "S1", "event_key": "men_speed_relay",
            "threshold_seconds": 9.60,
            "effective_from": iso(BASE - timedelta(days=30)),
            "issued_by": "tc", "ts": iso(BASE - timedelta(hours=2))})
        self.call("POST", "/admin/calibrations", {
            "cert_no": "CP", "lane": 4, "device_id": "tp4", "channel": "primary",
            "verified_by": "v", "issued_at": iso(BASE - timedelta(days=30)),
            "valid_from": iso(BASE - timedelta(days=10)),
            "valid_until": iso(BASE + timedelta(days=1)),
            "digest": "d1", "ts": iso(BASE - timedelta(hours=3))})
        self.call("POST", "/admin/calibrations", {
            "cert_no": "CB", "lane": 4, "device_id": "tb4", "channel": "backup",
            "verified_by": "v", "issued_at": iso(BASE - timedelta(days=30)),
            "valid_from": iso(BASE - timedelta(days=10)),
            "valid_until": iso(BASE + timedelta(days=1)),
            "digest": "d2", "ts": iso(BASE - timedelta(hours=3))})
        self.call("POST", "/admin/rounds", {
            "meeting_id": "M", "round_id": "F", "code": "final",
            "ts": iso(BASE - timedelta(hours=2))})
        self.call("POST", "/admin/teams", {
            "meeting_id": "M", "team_id": "CHN", "name": "中国队",
            "athlete_ids": ["zhao", "qian"], "ts": iso(BASE - timedelta(hours=2))})
        self.call("POST", "/admin/teams/lineup", {
            "team_id": "CHN", "round_id": "F", "athlete_ids": ["zhao", "qian"],
            "ts": iso(BASE - timedelta(hours=1))})
        self.call("POST", "/attempts", {
            "attempt_id": "A1", "meeting_id": "M", "round_id": "F",
            "team_id": "CHN", "lane": 4, "ts": iso(BASE)})

        # 双通道读数摄入；网络重传第二次必须 duplicated=true 且序号不增加
        values = {
            "reaction1": (0.120, 0.121), "split": (4.80, 4.802),
            "reaction2": (0.110, 0.111), "finish": (9.58, 9.582),
        }
        seqs = {"primary": 0, "backup": 0}
        devices = {"primary": "tp4", "backup": "tb4"}
        first_seq = None
        for i, (phase, (pv, bv)) in enumerate(values.items()):
            for channel, value in (("primary", pv), ("backup", bv)):
                seqs[channel] += 1
                payload = {
                    "phase": phase, "channel": channel, "value": value,
                    "ts": iso(BASE + timedelta(seconds=i)),
                    "device_id": devices[channel], "channel_seq": seqs[channel],
                    "idem_key": f"k-{phase}-{channel}"}
                status, body = self.call("POST", "/attempts/A1/readings", payload)
                self.assertEqual(status, 200, body)
                if phase == "finish" and channel == "primary":
                    first_seq = body["seq"]
        retransmit = {
            "phase": "finish", "channel": "primary", "value": 9.58,
            "ts": iso(BASE + timedelta(seconds=3)), "device_id": "tp4",
            "channel_seq": 99, "idem_key": "k-finish-primary"}
        status, body = self.call("POST", "/attempts/A1/readings", retransmit)
        self.assertEqual(status, 200)
        self.assertTrue(body["duplicated"])
        self.assertEqual(body["seq"], first_seq)

        # 缺感标记尝试（第二条尝试）演示待核不补值
        self.call("POST", "/attempts", {
            "attempt_id": "A2", "meeting_id": "M", "round_id": "F",
            "team_id": "CHN", "lane": 4, "ts": iso(BASE)})
        status, _ = self.call("POST", "/attempts/A2/missing", {
            "phase": "finish", "channel": "backup", "reason": "光电门离线",
            "ts": iso(BASE + timedelta(seconds=4)), "device_id": "tb4",
            "channel_seq": seqs["backup"] + 1, "idem_key": "miss-a2"})
        self.assertEqual(status, 200)
        status, live2 = self.call("GET", "/attempts/A2/live")
        self.assertEqual(live2["state"], "待核验")
        self.assertIsNone(live2["provisional_total"])

        # 三级发布：即时 -> 正式 -> 纪录
        status, live = self.call("GET", "/attempts/A1/live")
        self.assertEqual(status, 200)
        self.assertEqual(live["published"], "live")
        self.assertEqual(live["provisional_total"], 9.58)

        status, _ = self.call("GET", "/rounds/F/results")
        self.assertEqual(status, 422)  # 尚未正式化

        self.call("POST", "/attempts/A1/referee-confirmation",
                  {"by": "裁判-周", "ts": iso(BASE + timedelta(seconds=20))})
        status, body = self.call("POST", "/rounds/F/officialize",
                                 {"by": "裁判长", "ts": iso(BASE + timedelta(minutes=5))})
        self.assertEqual(status, 200, body)
        # 待核的 A2 被挡在正式赛果之外
        self.assertEqual([h["attempt_id"] for h in body["data"]["held_back"]], ["A2"])

        status, results = self.call("GET", "/rounds/F/results")
        self.assertEqual(results["state"], "正式赛果")
        self.assertEqual(results["results"][0]["attempt_id"], "A1")

        # 申诉期未到，核准被挡
        status, body = self.call("POST", "/records/ratify", {
            "attempt_id": "A1", "record_id": "WR-958",
            "event_key": "men_speed_relay", "by": "技术代表-马",
            "ts": iso(BASE + timedelta(minutes=10))})
        self.assertEqual(status, 422)
        self.assertIn("申诉截止", body["error"])

        status, body = self.call("POST", "/records/ratify", {
            "attempt_id": "A1", "record_id": "WR-958",
            "event_key": "men_speed_relay", "by": "技术代表-马",
            "ts": iso(BASE + timedelta(minutes=31))})
        self.assertEqual(status, 200, body)

        status, records = self.call("GET", "/records")
        self.assertEqual(records["records"][0]["official_time_seconds"], 9.58)

        # 从 9 秒 58 这一条记录取证据包
        status, evidence = self.call("GET", "/records/evidence",
                                     query={"time": "9.58"})
        self.assertEqual(status, 200)
        self.assertEqual(evidence["record"]["record_id"], "WR-958")
        self.assertEqual(evidence["climber_order"], ["zhao", "qian"])
        self.assertEqual(len(evidence["raw_segments"]), 8)
        self.assertEqual({c["cert_no"] for c in evidence["calibration_certificates"]},
                         {"CP", "CB"})
        self.assertEqual(evidence["referee_confirmation"]["by"], "裁判-周")
        self.assertFalse(evidence["protest_status"]["open"])
        self.assertTrue(evidence["seal"]["chain_verified"])

    def test_bad_payload_and_unknown_route(self):
        status, body = self.call("POST", "/admin/meetings", {"meeting_id": "M"})
        self.assertEqual(status, 400)
        self.assertIn("缺少必填字段", body["error"])
        status, _ = self.call("POST", "/nope", {"x": 1})
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
