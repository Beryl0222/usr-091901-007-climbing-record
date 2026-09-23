"""攀岩接力纪录核准服务入口。

对外接口分三级：
- GET  /attempts/{id}/live      即时成绩（未核对原始读数）
- GET  /rounds/{id}/results     正式赛果（裁判确认并正式化）
- GET  /records                 已核准纪录一览
- GET  /records/evidence        单条纪录的证据包（校准/原始分段/裁判/申诉）

写接口全部 POST JSON，每个命令封存为一条事件；读数摄入必须带 idem_key 与
channel_seq，重传幂等、乱序拒收。

运行：
  python3 service.py --check
  python3 service.py --port 8000 [--log-path data/sealed.jsonl]
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from domain import DomainError, RecordService
from eventlog import SealedLogError

SERVICE_ID = "climbing-record"
SERVICE_NAME = "攀岩接力纪录核准"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_service(log_path=None):
    """构造领域服务；log_path 指向 JSONL 封存文件（可重放恢复）。"""
    return RecordService(log_path=log_path)


# POST 路由 -> (领域方法名, 必填字段)
COMMAND_ROUTES = {
    "/admin/meetings": ("register_meeting",
                        ["meeting_id", "name", "date", "ts"]),
    "/admin/rounds": ("schedule_round",
                      ["meeting_id", "round_id", "code", "ts"]),
    "/admin/teams": ("register_team",
                     ["meeting_id", "team_id", "name", "athlete_ids", "ts"]),
    "/admin/teams/lineup": ("submit_lineup",
                            ["team_id", "round_id", "athlete_ids", "ts"]),
    "/admin/teams/lineup-change": ("change_lineup",
                                   ["team_id", "round_id", "athlete_ids",
                                    "reason", "ts"]),
    "/admin/calibrations": ("register_calibration", [
        "cert_no", "lane", "device_id", "channel", "verified_by",
        "issued_at", "valid_from", "valid_until", "digest", "ts"]),
    "/admin/standards": ("issue_record_standard", [
        "rule_id", "event_key", "threshold_seconds",
        "effective_from", "issued_by", "ts"]),
    "/admin/precision-rules": ("issue_precision_rule", [
        "rule_id", "channel_tolerance_seconds",
        "false_start_threshold_seconds", "timing_resolution",
        "effective_from", "issued_by", "ts"]),
    "/attempts": ("start_attempt", [
        "attempt_id", "meeting_id", "round_id", "team_id", "lane", "ts"]),
    "/protests": ("file_protest", [
        "protest_id", "meeting_id", "round_id", "attempt_id",
        "team_id", "reason", "ts"]),
    "/records/ratify": ("ratify_record", [
        "attempt_id", "record_id", "event_key", "by", "ts"]),
    "/records/reject": ("reject_record",
                        ["attempt_id", "by", "ts", "reason"]),
}

ATTEMPT_COMMAND_ROUTES = {
    "/referee-confirmation": ("referee_confirm", ["by", "ts"]),
    "/false-start": ("declare_false_start",
                     ["leg", "reaction_seconds", "by", "ts"]),
    "/false-start/revoke": ("revoke_false_start", ["by", "ts", "reason"]),
    "/rerun": ("grant_rerun", ["by", "ts", "reason"]),
    "/rerun/void": ("void_rerun", ["by", "ts", "reason"]),
    "/readings": ("ingest_reading", [
        "phase", "channel", "value", "ts", "device_id",
        "channel_seq", "idem_key"]),
    "/missing": ("mark_sensor_missing", [
        "phase", "channel", "reason", "ts", "device_id",
        "channel_seq", "idem_key"]),
    "/corrections": ("correct_result", ["by", "ts", "reason"]),
}

PROTEST_COMMAND_ROUTES = {
    "/resolve": ("resolve_protest", ["verdict", "by", "ts"]),
}

# 从请求体按方法形参名透传的可选字段
OPTIONAL_FIELDS = {
    "register_meeting": ["protest_window_seconds", "device_id", "idem_key"],
    "schedule_round": ["heat_no", "device_id", "idem_key"],
    "register_team": ["device_id", "idem_key"],
    "submit_lineup": ["device_id", "idem_key"],
    "change_lineup": ["device_id", "idem_key"],
    "register_calibration": ["idem_key"],
    "issue_record_standard": ["idem_key"],
    "issue_precision_rule": ["idem_key"],
    "start_attempt": ["device_id", "idem_key", "supersedes"],
    "ingest_reading": [],
    "mark_sensor_missing": [],
    "referee_confirm": ["note", "idem_key"],
    "declare_false_start": ["note", "idem_key"],
    "revoke_false_start": ["idem_key"],
    "grant_rerun": ["idem_key"],
    "void_rerun": ["idem_key"],
    "file_protest": ["idem_key"],
    "resolve_protest": ["note", "idem_key"],
    "correct_result": ["corrected_total", "corrected_checkpoints", "idem_key"],
    "ratify_record": ["idem_key"],
    "reject_record": ["idem_key"],
}


class Handler(BaseHTTPRequestHandler):
    """纪录核准服务的 HTTP 接口；默认使用进程内封存日志。"""

    service = build_service()

    # -- GET：三级发布接口 --------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._send_json(health_payload()); return
        if path == "/contract":
            self._send_json(load_contract()); return
        try:
            with self.service._service_lock:
                if path.startswith("/attempts/") and path.endswith("/live"):
                    attempt_id = path.split("/")[2]
                    self._send_json(self.service.live_result(attempt_id)); return
                if path.startswith("/rounds/") and path.endswith("/results"):
                    round_id = path.split("/")[2]
                    self._send_json(self.service.round_results(round_id)); return
                if path == "/records":
                    self._send_json(self.service.list_records()); return
                if path == "/records/evidence":
                    query = parse_qs(parsed.query)
                    self._send_json(self.service.record_evidence(
                        record_id=_one(query, "record_id"),
                        attempt_id=_one(query, "attempt_id"),
                        time_seconds=_float_or_none(_one(query, "time")))); return
            self.send_error(404)
        except DomainError as exc:
            self._send_json({"error": str(exc)}, status=422)

    # -- POST：命令封存 -----------------------------------------------------

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()
        if body is None:
            return
        try:
            method_name, required, positional = self._route(path, body)
            missing = [f for f in required if f not in body]
            if missing:
                self._send_json({"error": f"缺少必填字段：{', '.join(missing)}"},
                                status=400); return
            method = getattr(self.service, method_name)
            allowed = list(required) + OPTIONAL_FIELDS.get(method_name, [])
            kwargs = {k: body[k] for k in allowed if k in body}
            with self.service._service_lock:
                event, duplicated = method(*positional, **kwargs)
            self._send_json({
                "sealed": True, "duplicated": duplicated,
                "seq": event["seq"], "type": event["type"],
                "hash": event["hash"], "ts": event["ts"],
                "data": event["payload"],
            })
        except DomainError as exc:
            self._send_json({"error": str(exc)}, status=422)
        except SealedLogError as exc:
            self._send_json({"error": str(exc)}, status=422)
        except (TypeError, KeyError) as exc:
            self._send_json({"error": f"请求字段无效：{exc}"}, status=400)

    def _route(self, path, body):
        if path in COMMAND_ROUTES:
            method_name, required = COMMAND_ROUTES[path]
            return method_name, required, []
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[0] == "attempts":
            attempt_id, suffix = parts[1], "/" + parts[2]
            if suffix in ATTEMPT_COMMAND_ROUTES:
                method_name, required = ATTEMPT_COMMAND_ROUTES[suffix]
                return method_name, required, [attempt_id]
        if len(parts) == 3 and parts[0] == "protests":
            protest_id, suffix = parts[1], "/" + parts[2]
            if suffix in PROTEST_COMMAND_ROUTES:
                method_name, required = PROTEST_COMMAND_ROUTES[suffix]
                return method_name, required, [protest_id]
        if len(parts) == 3 and parts[0] == "rounds" and parts[2] == "officialize":
            return "officialize_round", ["by", "ts"], [parts[1]]
        raise DomainError(f"未知接口：{path}")

    # -- 工具 ---------------------------------------------------------------

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            self._send_json({"error": "请求体必须为 JSON 对象"}, status=400)
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            assert isinstance(body, dict)
            return body
        except (ValueError, AssertionError):
            self._send_json({"error": "请求体必须为 JSON 对象"}, status=400)
            return None

    def _send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return


def _one(query, key):
    values = query.get(key)
    return values[0] if values else None


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        raise DomainError(f"time 查询参数必须是秒数：{value}")


def self_check():
    """配置与契约自检。"""
    contract = load_contract()
    assert contract["states"] and contract["invariants"]
    service = build_service()
    assert service.log.verify_chain() is None
    assert {"预赛", "四分之一决赛", "半决赛", "决赛"} <= set(
        r["name"] for r in contract["rounds"])
    assert {"即时成绩", "正式赛果", "纪录已核准"} <= set(contract["states"])
    print("基础检查通过")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--log-path", default=None,
                        help="事件封存 JSONL 文件路径，重启后自动重放")
    args = parser.parse_args()
    if args.check:
        self_check(); return
    Handler.service = build_service(args.log_path)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
