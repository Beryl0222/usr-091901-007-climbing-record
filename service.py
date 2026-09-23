"""攀岩接力纪录核准服务入口。

GET 读模型按契约分为三类、彼此独立：

* ``/attempts/{id}/instant`` —— 即时成绩（大屏现场，不作为正式依据）
* ``/races/{id}/result``      —— 正式赛果（含判罚、更正、申诉状态）
* ``/records/{id}``           —— 已核准纪录（含完整核准档案）

写操作统一走 ``POST /commands``（``{"command": ..., ...}``），也提供语义化别名
路由；每条命令一次封存，全部幂等/冲突语义由领域核心决定。
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from domain import DomainError, RecordService

SERVICE_ID = "climbing-record"
SERVICE_NAME = "攀岩接力纪录核准"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")

# 命令 → (领域方法名, 位置参数名(按序), 可选/关键字参数名)
# 时间与操作人等一律以关键字传入，与领域方法的 keyword-only 签名一致。
COMMANDS = {
    "register-round": ("register_round", ["round_id"], ["at", "name"]),
    "register-race": ("register_race", ["race_id", "round_id"], ["at", "lane"]),
    "register-team": ("register_team", ["team_id"], ["at", "name"]),
    "announce-team": ("announce_team", ["race_id", "team_id", "climber_order"], ["at", "note"]),
    "register-attempt": ("register_attempt", ["attempt_id", "race_id", "team_id"],
                         ["at", "climber_order"]),
    "record-reading": ("record_reading",
                       ["attempt_id", "source", "reading_id", "reading_type",
                        "value", "occurred_at"],
                       ["channel", "device_id", "segment", "received_at"]),
    "mark-reading-missing": ("mark_reading_missing", ["attempt_id", "reading_type"],
                             ["at", "channel", "sensor_id", "note"]),
    "register-calibration": ("register_calibration",
                             ["cert_id", "device_id", "valid_from"],
                             ["at", "valid_to", "channel", "note"]),
    "revoke-calibration": ("revoke_calibration", ["cert_id"], ["at", "reason"]),
    "register-rule-version": ("register_rule_version", ["version"],
                              ["at", "effective_at", "record_threshold", "precision",
                               "expected_splits", "note"]),
    "judge-confirm": ("judge_confirm", ["attempt_id", "kind"],
                      ["at", "judge_id", "note", "detail"]),
    "file-appeal": ("file_appeal", ["race_id"],
                    ["at", "filed_by", "grounds", "appeal_id"]),
    "resolve-appeal": ("resolve_appeal", ["appeal_id"], ["at", "outcome", "note"]),
    "close-appeal-window": ("close_appeal_window", ["race_id"], ["at"]),
    "disqualify": ("disqualify", ["attempt_id"], ["at", "reason", "official_id"]),
    "rescind-disqualification": ("rescind_disqualification", ["attempt_id"],
                                 ["at", "reason", "official_id"]),
    "order-rerun": ("order_rerun", ["race_id", "replaced_attempt_ids"],
                    ["at", "reason"]),
    "correct-result": ("correct_result", ["attempt_id", "corrected_finish"],
                       ["at", "reason", "official_id"]),
    "propose-record": ("propose_record", ["attempt_id"], ["at", "proposal_id"]),
    "ratify-record": ("ratify_record", ["proposal_id"],
                      ["at", "technical_delegate"]),
    "reject-record": ("reject_record", ["proposal_id"], ["at", "reason"]),
}

# 语义化别名：(方法, 正则, 注入的命令与路径字段)
POST_ROUTES = [
    ("POST", re.compile(r"^/rounds/(?P<round_id>[^/]+)$"), "register-round"),
    ("POST", re.compile(r"^/rounds/(?P<round_id>[^/]+)/races$"), "register-race"),
    ("POST", re.compile(r"^/teams$"), "register-team"),
    ("POST", re.compile(r"^/races/(?P<race_id>[^/]+)/roster$"), "announce-team"),
    ("POST", re.compile(r"^/races/(?P<race_id>[^/]+)/attempts$"), "register-attempt"),
    ("POST", re.compile(r"^/attempts/(?P<attempt_id>[^/]+)/readings$"), "record-reading"),
    ("POST", re.compile(r"^/attempts/(?P<attempt_id>[^/]+)/missing$"),
     "mark-reading-missing"),
    ("POST", re.compile(r"^/calibrations$"), "register-calibration"),
    ("POST", re.compile(r"^/calibrations/(?P<cert_id>[^/]+)/revoke$"), "revoke-calibration"),
    ("POST", re.compile(r"^/rules$"), "register-rule-version"),
    ("POST", re.compile(r"^/attempts/(?P<attempt_id>[^/]+)/confirmations$"),
     "judge-confirm"),
    ("POST", re.compile(r"^/races/(?P<race_id>[^/]+)/appeals$"), "file-appeal"),
    ("POST", re.compile(r"^/appeals/(?P<appeal_id>[^/]+)/resolve$"), "resolve-appeal"),
    ("POST", re.compile(r"^/races/(?P<race_id>[^/]+)/appeal-window/close$"),
     "close-appeal-window"),
    ("POST", re.compile(r"^/attempts/(?P<attempt_id>[^/]+)/disqualify$"), "disqualify"),
    ("POST", re.compile(r"^/attempts/(?P<attempt_id>[^/]+)/rescind-dq$"),
     "rescind-disqualification"),
    ("POST", re.compile(r"^/races/(?P<race_id>[^/]+)/reruns$"), "order-rerun"),
    ("POST", re.compile(r"^/attempts/(?P<attempt_id>[^/]+)/corrections$"),
     "correct-result"),
    ("POST", re.compile(r"^/attempts/(?P<attempt_id>[^/]+)/record-proposal$"),
     "propose-record"),
    ("POST", re.compile(r"^/records/(?P<proposal_id>[^/]+)/ratify$"), "ratify-record"),
    ("POST", re.compile(r"^/records/(?P<proposal_id>[^/]+)/reject$"), "reject-record"),
]


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# 默认全局实例；测试与多实例部署可替换/自建。
service = RecordService()


def execute_command(service_obj, command, payload):
    """按命令声明把 JSON 载荷映射为领域方法调用。"""
    if command not in COMMANDS:
        raise DomainError(f"未知命令: {command}", code="missing")
    method_name, positional, optional = COMMANDS[command]
    merged = dict(payload)
    missing = [name for name in positional if name not in merged]
    if "at" in optional and "at" not in merged:
        missing.append("at")
    if missing:
        raise DomainError(f"命令 {command} 缺少字段: {', '.join(missing)}")
    args = [merged[name] for name in positional]
    kwargs = {name: merged[name] for name in optional if name in merged}
    try:
        return getattr(service_obj, method_name)(*args, **kwargs)
    except TypeError as exc:
        # 领域方法签名层面的参数问题统一按 400 返回，避免连接被直接断开。
        raise DomainError(f"命令 {command} 参数不合法: {exc}") from exc


class Handler(BaseHTTPRequestHandler):
    """命令写入 + 三类读模型接口。"""

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/health":
                self._send_json(health_payload()); return
            if path == "/contract":
                self._send_json(load_contract()); return
            if path == "/records":
                self._send_json({"records": service.list_records(query.get("status", [None])[0])}); return
            if path == "/events":
                from_seq = int(query.get("from_seq", ["0"])[0])
                self._send_json({"events": service.events(from_seq)}); return
            m = re.fullmatch(r"/attempts/(?P<id>[^/]+)/instant", path)
            if m:
                self._send_json(service.instant_view(m.group("id"))); return
            m = re.fullmatch(r"/races/(?P<id>[^/]+)/result", path)
            if m:
                self._send_json(service.official_view(m.group("id"))); return
            m = re.fullmatch(r"/records/(?P<id>[^/]+)", path)
            if m:
                self._send_json(service.record_view(m.group("id"))); return
            self.send_error(404)
        except DomainError as exc:
            self._send_error(exc)
        except ValueError as exc:
            self._send_json({"error": str(exc), "code": "invalid"}, status=400)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._read_body()
            command, payload = self._route(parsed.path, body)
            result = execute_command(service, command, payload)
            self._send_json({"command": command, "result": result,
                             "sealed_events": service.sealed_count}, status=201)
        except DomainError as exc:
            self._send_error(exc)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": f"请求体不是合法 JSON: {exc}", "code": "invalid"},
                            status=400)

    def _route(self, path, body):
        if not isinstance(body, dict):
            raise DomainError("请求体必须是 JSON 对象")
        if path == "/commands":
            command = body.get("command")
            if not command:
                raise DomainError("缺少 command 字段")
            payload = {k: v for k, v in body.items() if k != "command"}
            return command, payload
        for _method, pattern, command in POST_ROUTES:
            match = pattern.fullmatch(path)
            if match:
                payload = dict(body)
                payload.update(match.groupdict())
                return command, payload
        raise DomainError(f"未知路由: {path}", code="missing")

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_error(self, exc):
        status = {"invalid": 400, "missing": 404, "conflict": 409}.get(exc.code, 400)
        self._send_json({"error": str(exc), "code": exc.code,
                         "reasons": exc.reasons}, status=status)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        assert len(contract["rounds"]) == 4
        assert len(contract["read_models"]) == 3
        assert all(cmd in contract["commands"] for cmd in COMMANDS)
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
