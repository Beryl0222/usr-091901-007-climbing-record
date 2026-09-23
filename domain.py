"""攀岩接力纪录核准领域核心。

设计原则（与 domain_contract.json 一一对应）：

* 所有输入都落成只追加事件，封存顺序按 (发生时间, 接收序号)；网络重传按
  ``(来源, 读数标识)`` 幂等去重，不会产生第二次有效攀爬。
* 双通道计时分别留痕，绝不合并；传感器缺失只登记 ``reading-marked-missing``
  并把尝试置于“待核验”，系统任何路径都不会自动补值。
* 预赛、四分之一决赛、半决赛、决赛各自独立身份；换人、重赛、犯规与撤销、
  成绩更正全部以追加事件保留前因后果，原读数永不删除。
* 纪录标准按“尝试发生时”已生效的规则版本选取，规则更新不溯及既往。
* 即时成绩 / 正式赛果 / 已核准纪录是三个互不相同的读模型；任一已核准纪录
  都能回溯校准证书、原始分段、裁判确认与申诉截止状态。
"""

from __future__ import annotations

import threading
from decimal import Decimal, ROUND_HALF_UP

ROUNDS = ("heat", "quarterfinal", "semifinal", "final")
ROUND_NAMES = {
    "heat": "预赛",
    "quarterfinal": "四分之一决赛",
    "semifinal": "半决赛",
    "final": "决赛",
}
CHANNELS = ("primary", "backup")
TIMER_SOURCES = {
    "timer-primary": "primary",
    "timer-backup": "backup",
}
SOURCES = ("timer-primary", "timer-backup", "referee-terminal", "device-inspection")
READING_TYPES = ("reaction", "start-gate", "split", "handoff", "finish")
CHANNEL_REQUIRED_TYPES = ("start-gate", "split", "finish")
APPEAL_OUTCOMES = ("upheld", "rejected", "withdrawn")


class DomainError(Exception):
    """业务规则冲突。``code`` 映射为 HTTP 状态：invalid→400 / missing→404 / conflict→409。"""

    def __init__(self, message, code="invalid", reasons=None):
        super().__init__(message)
        self.code = code
        self.reasons = reasons or []


def _decimal(value, label="数值"):
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise DomainError(f"{label}不是合法数字: {value!r}") from exc


def _quantize(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


class RecordService:
    """纪录核准服务的内存领域实现（线程安全，事件只追加）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._seq = 0
        self._events = []
        self._rounds = {}
        self._races = {}
        self._teams = {}
        self._attempts = {}
        self._calibrations = {}
        self._rules = []  # 注册顺序保存，按 effective_at 选取
        self._records = {}
        self._appeals = {}

    @property
    def sealed_count(self):
        """已封存事件总数（只增不减）。"""
        return self._seq

    # ------------------------------------------------------------------ 基础

    def _append(self, event_type, at, data):
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            raise DomainError("occurred_at 必须是数字时间戳")
        self._seq += 1
        event = {"seq": self._seq, "event": event_type, "occurred_at": at, "data": dict(data)}
        self._events.append(event)
        return event

    def _require_attempt(self, attempt_id):
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            raise DomainError(f"尝试不存在: {attempt_id}", code="missing")
        return attempt

    def _require_race(self, race_id):
        race = self._races.get(race_id)
        if race is None:
            raise DomainError(f"场次不存在: {race_id}", code="missing")
        return race

    # ----------------------------------------------------------- 赛会登记命令

    def register_round(self, round_id, *, at, name=None):
        with self._lock:
            if round_id not in ROUNDS:
                raise DomainError(f"未知轮次身份: {round_id}，合法值: {', '.join(ROUNDS)}")
            if round_id in self._rounds:
                raise DomainError(f"轮次已登记: {round_id}", code="conflict")
            self._append("round-registered", at, {
                "round_id": round_id, "name": name or ROUND_NAMES[round_id],
            })
            self._rounds[round_id] = {"id": round_id, "name": name or ROUND_NAMES[round_id]}
            return {"round_id": round_id}

    def register_race(self, race_id, round_id, *, at, lane=None):
        with self._lock:
            if round_id not in self._rounds:
                raise DomainError(f"轮次尚未登记: {round_id}", code="missing")
            if race_id in self._races:
                raise DomainError(f"场次已登记: {race_id}", code="conflict")
            self._append("race-scheduled", at, {
                "race_id": race_id, "round_id": round_id, "lane": lane,
            })
            self._races[race_id] = {
                "id": race_id, "round_id": round_id, "lane": lane,
                "roster": [], "attempts": [], "reruns": [],
                "appeals": [], "appeal_window_closed_at": None,
            }
            return {"race_id": race_id}

    def register_team(self, team_id, *, at, name):
        with self._lock:
            if team_id in self._teams:
                raise DomainError(f"队伍已登记: {team_id}", code="conflict")
            self._append("team-registered", at, {"team_id": team_id, "name": name})
            self._teams[team_id] = {"id": team_id, "name": name}
            return {"team_id": team_id}

    def announce_team(self, race_id, team_id, climber_order, *, at, note=""):
        """出场名单（含换人）。每次宣布都留痕，历史名单保留不覆盖。"""
        with self._lock:
            race = self._require_race(race_id)
            if team_id not in self._teams:
                raise DomainError(f"队伍尚未登记: {team_id}", code="missing")
            climber_order = list(climber_order or [])
            if len(climber_order) != 2 or len(set(climber_order)) != 2:
                raise DomainError("接力出场名单必须是两名不同选手")
            previous = race["roster"][-1]["climber_order"] if race["roster"] else None
            event = self._append("team-announced", at, {
                "race_id": race_id, "team_id": team_id,
                "climber_order": climber_order, "previous_climber_order": previous,
                "substitution": previous is not None and previous != climber_order,
                "note": note,
            })
            race["roster"].append({"event_seq": event["seq"], "team_id": team_id,
                                   "climber_order": climber_order, "note": note})
            return {"race_id": race_id, "climber_order": climber_order,
                    "substitution": event["data"]["substitution"]}

    def register_attempt(self, attempt_id, race_id, team_id, *, at, climber_order=None):
        with self._lock:
            race = self._require_race(race_id)
            if team_id not in self._teams:
                raise DomainError(f"队伍尚未登记: {team_id}", code="missing")
            if attempt_id in self._attempts:
                raise DomainError(f"尝试已登记: {attempt_id}", code="conflict")
            order = list(climber_order) if climber_order is not None else None
            if order is None:
                announced = [r for r in race["roster"] if r["team_id"] == team_id]
                if not announced:
                    raise DomainError("队伍未宣布出场名单，且尝试未自带接力顺序")
                order = announced[-1]["climber_order"]
            if len(order) != 2 or len(set(order)) != 2:
                raise DomainError("必须登记两名不同选手的接力顺序")
            self._append("attempt-registered", at, {
                "attempt_id": attempt_id, "race_id": race_id,
                "round_id": race["round_id"], "team_id": team_id,
                "climber_order": order,
            })
            attempt = {
                "id": attempt_id, "race_id": race_id, "round_id": race["round_id"],
                "team_id": team_id, "climber_order": order, "at": at,
                "readings": {}, "channel_readings": {"primary": [], "backup": []},
                "referee_readings": [], "missing": [],
                "confirmations": [], "adjudicated_missing": {},
                "status": "即时成绩", "finalized_at": None,
                "disqualifications": [], "corrections": [],
                "superseded": False, "supersede_reason": None,
                "rule_version": None, "proposal_id": None,
            }
            self._attempts[attempt_id] = attempt
            race["attempts"].append(attempt_id)
            return {"attempt_id": attempt_id, "climber_order": order}

    # --------------------------------------------------------------- 计时读数

    def record_reading(self, attempt_id, source, reading_id, reading_type, value,
                       occurred_at, *, channel=None, device_id=None, segment=None,
                       received_at=None):
        """封存一条原始读数。重传同一 ``(source, reading_id)`` 直接返回原事件。"""
        with self._lock:
            attempt = self._require_attempt(attempt_id)
            self._require_mutable(attempt, "追加读数")
            key = (source, reading_id)
            existing = attempt["readings"].get(key)
            if existing is not None:
                # 网络重传：不追加事件、不增加有效攀爬，原样返回第一次封存的读数。
                # 去重先于一切字段校验，保证迟到/不完整的重传包也被吞掉。
                return {"duplicate": True, "seq": existing["seq"],
                        "attempt_id": attempt_id, "reading_id": reading_id}
            if source not in SOURCES:
                raise DomainError(f"未知读数来源: {source}")
            if reading_type not in READING_TYPES:
                raise DomainError(f"未知读数类型: {reading_type}")
            if source in TIMER_SOURCES:
                channel = TIMER_SOURCES[source]
            if reading_type in CHANNEL_REQUIRED_TYPES and channel not in CHANNELS:
                raise DomainError(f"{reading_type} 读数必须来自计时双通道之一并标明 channel")
            if reading_type in CHANNEL_REQUIRED_TYPES and not device_id:
                raise DomainError(f"{reading_type} 读数必须携带 device_id 以便核对校准")
            if value is not None:
                _decimal(value, "读数")
            event = self._append("reading-recorded", occurred_at, {
                "attempt_id": attempt_id, "source": source, "reading_id": reading_id,
                "reading_type": reading_type, "channel": channel,
                "device_id": device_id, "segment": segment,
                "value": value, "occurred_at": occurred_at,
                "received_at": received_at if received_at is not None else occurred_at,
            })
            record = dict(event["data"], seq=event["seq"])
            attempt["readings"][key] = record
            if channel in CHANNELS:
                attempt["channel_readings"][channel].append(record)
            else:
                attempt["referee_readings"].append(record)
            return {"duplicate": False, "seq": event["seq"],
                    "attempt_id": attempt_id, "reading_id": reading_id}

    def mark_reading_missing(self, attempt_id, reading_type, *, at, channel=None,
                             sensor_id=None, note=""):
        """传感器缺失：只能标待核，绝不补值。"""
        with self._lock:
            attempt = self._require_attempt(attempt_id)
            self._require_mutable(attempt, "登记传感器缺失")
            if reading_type not in READING_TYPES:
                raise DomainError(f"未知读数类型: {reading_type}")
            if reading_type in CHANNEL_REQUIRED_TYPES and channel not in CHANNELS:
                raise DomainError(f"{reading_type} 缺失登记必须标明 primary/backup 通道")
            marker = {"reading_type": reading_type, "channel": channel,
                      "sensor_id": sensor_id, "note": note}
            if any(m["reading_type"] == reading_type and m["channel"] == channel
                   and m["sensor_id"] == sensor_id for m in attempt["missing"]):
                raise DomainError("该传感器缺失已登记，禁止重复登记", code="conflict")
            event = self._append("reading-marked-missing", at, {
                "attempt_id": attempt_id, **marker,
            })
            marker["event_seq"] = event["seq"]
            attempt["missing"].append(marker)
            if attempt["status"] in ("即时成绩", "正式赛果"):
                # 新发现的缺失会把已确认赛果打回待核；申诉中则保持申诉中。
                attempt["status"] = "待核验"
            return {"attempt_id": attempt_id, "status": attempt["status"]}

    # ----------------------------------------------------------- 校准与规则

    def register_calibration(self, cert_id, device_id, valid_from, *, at,
                             valid_to=None, channel=None, note=""):
        with self._lock:
            if cert_id in self._calibrations:
                raise DomainError(f"校准证书编号已存在: {cert_id}", code="conflict")
            vf = _decimal(valid_from, "valid_from")
            vt = _decimal(valid_to, "valid_to") if valid_to is not None else None
            if vt is not None and vt <= vf:
                raise DomainError("校准有效期结束时间必须晚于开始时间")
            event = self._append("calibration-registered", at, {
                "cert_id": cert_id, "device_id": device_id, "channel": channel,
                "valid_from": float(vf), "valid_to": float(vt) if vt is not None else None,
                "revoked": False, "note": note,
            })
            self._calibrations[cert_id] = {
                "cert_id": cert_id, "device_id": device_id, "channel": channel,
                "valid_from": vf, "valid_to": vt, "revoked": False,
                "registered_seq": event["seq"], "revoked_seq": None, "revoke_reason": None,
                "note": note,
            }
            return {"cert_id": cert_id}

    def revoke_calibration(self, cert_id, *, at, reason=""):
        with self._lock:
            cert = self._calibrations.get(cert_id)
            if cert is None:
                raise DomainError(f"校准证书不存在: {cert_id}", code="missing")
            if cert["revoked"]:
                raise DomainError("校准证书已撤销", code="conflict")
            event = self._append("calibration-revoked", at,
                                 {"cert_id": cert_id, "reason": reason})
            cert["revoked"] = True
            cert["revoked_seq"] = event["seq"]
            cert["revoke_reason"] = reason
            return {"cert_id": cert_id, "revoked": True}

    def register_rule_version(self, version, *, at, effective_at,
                              record_threshold, precision, expected_splits=0, note=""):
        with self._lock:
            if any(r["version"] == version for r in self._rules):
                raise DomainError(f"规则版本已存在: {version}", code="conflict")
            if not isinstance(precision, int) or precision < 0:
                raise DomainError("precision 必须是非负整数")
            threshold = _decimal(record_threshold, "纪录标准")
            if not isinstance(effective_at, (int, float)) or isinstance(effective_at, bool):
                raise DomainError("effective_at 必须是数字时间戳")
            self._append("rule-version-registered", at, {
                "version": version, "effective_at": effective_at,
                "record_threshold": float(threshold), "precision": precision,
                "expected_splits": expected_splits, "note": note,
            })
            self._rules.append({"version": version, "effective_at": effective_at,
                                "threshold": threshold, "precision": precision,
                                "expected_splits": expected_splits, "note": note})
            return {"version": version, "effective_at": effective_at}

    def _rule_for(self, attempt):
        """选取尝试发生时已生效的规则；规则更新不溯及既往。"""
        candidates = [r for r in self._rules if r["effective_at"] <= attempt["at"]]
        if not candidates:
            raise DomainError("尝试发生时没有已生效的纪录标准，不能提报纪录",
                              code="conflict")
        return max(candidates, key=lambda r: r["effective_at"])

    # --------------------------------------------------------------- 裁判/申诉

    def judge_confirm(self, attempt_id, kind, *, at, judge_id, note="", detail=None):
        with self._lock:
            attempt = self._require_attempt(attempt_id)
            self._require_mutable(attempt, "裁判确认")
            valid_kinds = ("start", "handoff-order", "result", "adjudicate-missing")
            if kind not in valid_kinds:
                raise DomainError(f"未知裁判确认类型: {kind}")
            detail = detail or {}
            if kind == "handoff-order":
                order = detail.get("climber_order")
                if not order or list(order) != attempt["climber_order"]:
                    raise DomainError("裁判确认的接力顺序必须与登记顺序完全一致")
            adjudication_seq = None
            if kind == "adjudicate-missing":
                mt = detail.get("reading_type")
                ch = detail.get("channel")
                match = [m for m in attempt["missing"]
                         if m["reading_type"] == mt and m["channel"] == ch]
                if not match:
                    raise DomainError("人工核验必须针对一条已登记的传感器缺失")
                key = f"{mt}:{ch}"
                if key in attempt["adjudicated_missing"]:
                    raise DomainError("该传感器缺失已完成人工核验", code="conflict")
            event = self._append("judgement-confirmed", at, {
                "attempt_id": attempt_id, "kind": kind, "judge_id": judge_id,
                "note": note, "detail": detail,
            })
            if kind == "adjudicate-missing":
                # 只记录“人工核验过、无需重赛/补测”，系统绝不写入任何补出来的数值。
                key = f"{detail['reading_type']}:{detail['channel']}"
                attempt["adjudicated_missing"][key] = {
                    "judge_id": judge_id, "note": note, "seq": event["seq"],
                }
            if kind == "result":
                unresolved = [f"{m['reading_type']}:{m['channel']}" for m in attempt["missing"]
                              if f"{m['reading_type']}:{m['channel']}"
                              not in attempt["adjudicated_missing"]]
                if unresolved:
                    raise DomainError("仍有传感器待核，裁判不得确认正式成绩",
                                      code="conflict", reasons=unresolved)
                race = self._races[attempt["race_id"]]
                pending_appeal = any(self._appeals[a]["outcome"] is None
                                     for a in race["appeals"])
                attempt["status"] = "申诉中" if pending_appeal else "正式赛果"
                attempt["finalized_at"] = at
            attempt["confirmations"].append({
                "seq": event["seq"], "kind": kind, "judge_id": judge_id,
                "note": note, "detail": detail, "occurred_at": at,
            })
            return {"attempt_id": attempt_id, "kind": kind, "seq": event["seq"]}

    @staticmethod
    def _require_mutable(attempt, action):
        if attempt["status"] == "纪录已核准":
            raise DomainError(f"尝试已随纪录核准封存，禁止{action}", code="conflict")

    def file_appeal(self, race_id, *, at, filed_by, grounds, appeal_id=None):
        with self._lock:
            race = self._require_race(race_id)
            if race["appeal_window_closed_at"] is not None:
                raise DomainError("申诉窗口已截止，禁止再受理申诉", code="conflict")
            if any(self._attempts[a]["status"] == "纪录已核准"
                   for a in race["attempts"]):
                raise DomainError("本场次已有核准纪录，禁止再受理申诉", code="conflict")
            appeal_id = appeal_id or f"appeal-{len(race['appeals']) + 1}-{race_id}"
            if self._appeals.get(appeal_id):
                raise DomainError(f"申诉已存在: {appeal_id}", code="conflict")
            event = self._append("appeal-filed", at, {
                "appeal_id": appeal_id, "race_id": race_id,
                "filed_by": filed_by, "grounds": grounds,
            })
            appeal = {"id": appeal_id, "race_id": race_id, "filed_by": filed_by,
                      "grounds": grounds, "filed_at": at, "event_seq": event["seq"],
                      "resolved_at": None, "outcome": None, "note": None}
            self._appeals[appeal_id] = appeal
            race["appeals"].append(appeal_id)
            for aid in race["attempts"]:
                if self._attempts[aid]["status"] in ("即时成绩", "待核验", "正式赛果"):
                    self._attempts[aid]["status"] = "申诉中"
            return {"appeal_id": appeal_id}

    def resolve_appeal(self, appeal_id, *, at, outcome, note=""):
        with self._lock:
            appeal = self._appeals.get(appeal_id)
            if appeal is None:
                raise DomainError(f"申诉不存在: {appeal_id}", code="missing")
            if appeal["outcome"] is not None:
                raise DomainError("申诉已裁决", code="conflict")
            if outcome not in APPEAL_OUTCOMES:
                raise DomainError(f"未知裁决结果: {outcome}")
            self._append("appeal-resolved", at, {
                "appeal_id": appeal_id, "outcome": outcome, "note": note,
            })
            appeal["outcome"] = outcome
            appeal["resolved_at"] = at
            appeal["note"] = note
            race = self._races[appeal["race_id"]]
            if all(self._appeals[a]["outcome"] is not None for a in race["appeals"]):
                for aid in race["attempts"]:
                    attempt = self._attempts[aid]
                    if attempt["status"] != "申诉中":
                        continue
                    attempt["status"] = "正式赛果" if attempt["finalized_at"] else (
                        "待核验" if attempt["missing"] else "即时成绩")
            return {"appeal_id": appeal_id, "outcome": outcome}

    def close_appeal_window(self, race_id, *, at):
        with self._lock:
            race = self._require_race(race_id)
            if race["appeal_window_closed_at"] is not None:
                raise DomainError("申诉窗口已关闭", code="conflict")
            self._append("appeal-window-closed", at, {"race_id": race_id, "closed_at": at})
            race["appeal_window_closed_at"] = at
            return {"race_id": race_id, "closed_at": at}

    # ----------------------------------------------------- 犯规/重赛/成绩更正

    def disqualify(self, attempt_id, *, at, reason, official_id):
        with self._lock:
            attempt = self._require_attempt(attempt_id)
            self._require_mutable(attempt, "判罚犯规")
            if attempt["disqualifications"] and attempt["disqualifications"][-1]["active"]:
                raise DomainError("该尝试已处于犯规状态", code="conflict")
            event = self._append("disqualification-decided", at, {
                "attempt_id": attempt_id, "reason": reason, "official_id": official_id,
            })
            attempt["disqualifications"].append({
                "seq": event["seq"], "active": True, "reason": reason,
                "official_id": official_id, "at": at, "rescind_seq": None,
            })
            return {"attempt_id": attempt_id, "disqualified": True}

    def rescind_disqualification(self, attempt_id, *, at, reason, official_id):
        with self._lock:
            attempt = self._require_attempt(attempt_id)
            self._require_mutable(attempt, "撤销犯规")
            active = [d for d in attempt["disqualifications"] if d["active"]]
            if not active:
                raise DomainError("该尝试没有生效中的犯规判罚", code="conflict")
            event = self._append("disqualification-rescinded", at, {
                "attempt_id": attempt_id, "reason": reason, "official_id": official_id,
                "disqualification_seq": active[-1]["seq"],
            })
            active[-1]["active"] = False
            active[-1]["rescind_seq"] = event["seq"]
            active[-1]["rescind_reason"] = reason
            return {"attempt_id": attempt_id, "disqualified": False}

    def order_rerun(self, race_id, replaced_attempt_ids, *, at, reason):
        with self._lock:
            race = self._require_race(race_id)
            targets = []
            for aid in replaced_attempt_ids:
                attempt = self._attempts.get(aid)
                if attempt is None or attempt["race_id"] != race_id:
                    raise DomainError(f"重赛目标不属于本场次: {aid}", code="missing")
                self._require_mutable(attempt, "安排重赛")
                targets.append(aid)
            self._append("rerun-ordered", at, {
                "race_id": race_id, "replaced_attempt_ids": targets, "reason": reason,
            })
            for aid in targets:
                # 只标记取代、永不删除：原始读数与判罚全部留在封存链里。
                self._attempts[aid]["superseded"] = True
                self._attempts[aid]["supersede_reason"] = f"rerun: {reason}"
            race["reruns"].append({"at": at, "replaced": targets, "reason": reason})
            return {"race_id": race_id, "replaced": targets}

    def correct_result(self, attempt_id, corrected_finish, *, at, reason, official_id):
        with self._lock:
            attempt = self._require_attempt(attempt_id)
            self._require_mutable(attempt, "更正成绩")
            new_value = _decimal(corrected_finish, "更正成绩")
            previous = attempt["corrections"][-1]["new_value"] \
                if attempt["corrections"] else self._raw_finish(attempt)
            if previous is None:
                raise DomainError("缺少原始到顶读数，无法更正成绩", code="conflict")
            event = self._append("result-corrected", at, {
                "attempt_id": attempt_id, "previous_finish": float(previous),
                "corrected_finish": float(new_value), "reason": reason,
                "official_id": official_id,
            })
            attempt["corrections"].append({
                "seq": event["seq"], "previous_value": previous,
                "new_value": new_value, "reason": reason,
                "official_id": official_id, "at": at,
            })
            return {"attempt_id": attempt_id, "corrected_finish": float(new_value)}

    # --------------------------------------------------------------- 纪录核准

    def _reading_chain(self, attempt):
        items = list(attempt["readings"].values())
        return sorted(items, key=lambda r: (r["occurred_at"], r["seq"]))

    @staticmethod
    def _raw_finish(attempt, channel="primary"):
        finishes = [r for r in attempt["channel_readings"][channel]
                    if r["reading_type"] == "finish"]
        if not finishes:
            return None
        return _decimal(finishes[-1]["value"])

    def _official_finish(self, attempt):
        if attempt["corrections"]:
            return attempt["corrections"][-1]["new_value"], True
        return self._raw_finish(attempt), False

    def _evidence_issues(self, attempt):
        """核对除校准/申诉窗口外的证据完整性。"""
        issues = []
        for channel in CHANNELS:
            if not [r for r in attempt["channel_readings"][channel]
                    if r["reading_type"] == "finish"]:
                if f"finish:{channel}" in attempt["adjudicated_missing"]:
                    # 冗余通道故障经裁判人工核验：接受现存通道证据，但系统从未补值。
                    continue
                issues.append(f"{channel} 通道缺少到顶读数")
        if not [r for r in attempt["referee_readings"] if r["reading_type"] == "reaction"]:
            issues.append("缺少起跑反应读数")
        expected = attempt.get("rule_expected_splits", 0)
        for channel in CHANNELS:
            have = len([r for r in attempt["channel_readings"][channel]
                        if r["reading_type"] == "split"])
            covered_missing = len([
                m for m in attempt["missing"]
                if m["reading_type"] == "split" and m["channel"] == channel
                and f"split:{channel}" in attempt["adjudicated_missing"]])
            if have + covered_missing < expected:
                issues.append(f"{channel} 通道分段证据不足: {have}/{expected}"
                              f"（人工核验缺失 {covered_missing} 处，不含补值）")
        if len(attempt["climber_order"]) != 2:
            issues.append("两名选手的接力顺序不明")
        if not [c for c in attempt["confirmations"] if c["kind"] == "handoff-order"]:
            issues.append("缺少裁判对接力顺序的确认")
        for marker in attempt["missing"]:
            key = f"{marker['reading_type']}:{marker['channel']}"
            if key not in attempt["adjudicated_missing"]:
                issues.append(
                    f"传感器待核且未经人工核验: {marker['reading_type']}"
                    f"/{marker['channel'] or '-'}")
        if [d for d in attempt["disqualifications"] if d["active"]]:
            issues.append("存在生效中的犯规判罚")
        if attempt["superseded"]:
            issues.append("该尝试已被重赛取代")
        if not [c for c in attempt["confirmations"] if c["kind"] == "result"]:
            issues.append("缺少裁判对成绩的最终确认")
        return issues

    def _calibration_issues(self, attempt):
        issues = []
        certs = []
        devices = sorted({(r["device_id"], r["channel"])
                          for ch in CHANNELS
                          for r in attempt["channel_readings"][ch]
                          if r["reading_type"] in CHANNEL_REQUIRED_TYPES})
        at = attempt["at"]
        for device_id, channel in devices:
            cover = [c for c in self._calibrations.values()
                     if c["device_id"] == device_id and not c["revoked"]
                     and c["valid_from"] <= at
                     and (c["valid_to"] is None or at < c["valid_to"])]
            if not cover:
                issues.append(f"设备 {device_id}（{channel}）在尝试发生时无有效校准证书")
            else:
                certs.extend(c["cert_id"] for c in cover)
        return issues, sorted(set(certs)), devices

    def propose_record(self, attempt_id, *, at, proposal_id=None):
        with self._lock:
            attempt = self._require_attempt(attempt_id)
            if attempt["proposal_id"]:
                raise DomainError("该尝试已提报纪录", code="conflict")
            rule = self._rule_for(attempt)
            attempt["rule_version"] = rule["version"]
            attempt["rule_expected_splits"] = rule["expected_splits"]
            finish, corrected = self._official_finish(attempt)
            if finish is None:
                raise DomainError("缺少到顶读数，不能提报纪录", code="conflict")
            # 提案阶段的硬性资格：尝试本身必须“干净”。裁判确认、申诉窗口等
            # 程序性事项留给核准环节逐项核对（提案可先挂起为待核）。
            if [d for d in attempt["disqualifications"] if d["active"]]:
                raise DomainError("尝试存在生效中的犯规判罚，不能提报纪录",
                                  code="conflict")
            if attempt["superseded"]:
                raise DomainError("尝试已被重赛取代，不能提报纪录", code="conflict")
            pending_missing = [
                f"{m['reading_type']}:{m['channel']}" for m in attempt["missing"]
                if f"{m['reading_type']}:{m['channel']}"
                not in attempt["adjudicated_missing"]]
            if pending_missing:
                raise DomainError("存在未经人工核验的传感器缺失，不能提报纪录",
                                  code="conflict", reasons=pending_missing)
            quantized = _quantize(finish, rule["precision"])
            threshold_q = _quantize(rule["threshold"], rule["precision"])
            if quantized > threshold_q:
                raise DomainError(
                    f"成绩 {quantized} 未达到纪录标准 {threshold_q}", code="conflict")
            comparison = "tied" if quantized == threshold_q else "broken"
            proposal_id = proposal_id or f"rec-{attempt_id}"
            self._append("record-proposed", at, {
                "proposal_id": proposal_id, "attempt_id": attempt_id,
                "race_id": attempt["race_id"], "round_id": attempt["round_id"],
                "team_id": attempt["team_id"], "rule_version": rule["version"],
                "finish_time": float(finish), "precision": rule["precision"],
                "threshold": float(rule["threshold"]),
                "comparison": comparison, "corrected": corrected,
            })
            attempt["proposal_id"] = proposal_id
            self._records[proposal_id] = {
                "id": proposal_id, "attempt_id": attempt_id, "status": "eligible",
                "proposed_at": at, "rule_version": rule["version"],
                "finish_time": float(finish), "comparison": comparison,
                "ratified_at": None, "technical_delegate": None,
            }
            return {"proposal_id": proposal_id, "status": "eligible",
                    "comparison": comparison, "finish_time": float(finish)}

    def ratify_record(self, proposal_id, *, at, technical_delegate):
        with self._lock:
            record = self._records.get(proposal_id)
            if record is None:
                raise DomainError(f"纪录提案不存在: {proposal_id}", code="missing")
            if record["status"] == "ratified":
                raise DomainError("纪录已核准，禁止重复签发", code="conflict")
            if record["status"] == "rejected":
                raise DomainError("纪录已被驳回", code="conflict")
            attempt = self._require_attempt(record["attempt_id"])
            blockers = self._evidence_issues(attempt)
            cal_issues, cert_ids, devices = self._calibration_issues(attempt)
            blockers.extend(cal_issues)
            race = self._races[attempt["race_id"]]
            if race["appeal_window_closed_at"] is None:
                blockers.append("申诉窗口尚未截止")
            unresolved = [a for a in race["appeals"]
                          if self._appeals[a]["outcome"] is None]
            if unresolved:
                blockers.append(f"存在未决申诉: {', '.join(unresolved)}")
            if blockers:
                record["status"] = "pending-verification"
                raise DomainError("纪录暂不能核准，存在待核事项", code="conflict",
                                  reasons=blockers)
            primary = self._raw_finish(attempt, "primary")
            backup = self._raw_finish(attempt, "backup")
            event = self._append("record-ratified", at, {
                "proposal_id": proposal_id, "attempt_id": attempt["id"],
                "technical_delegate": technical_delegate,
                "finish_time": record["finish_time"],
                "rule_version": record["rule_version"],
                "primary_finish": float(primary) if primary is not None else None,
                "backup_finish": float(backup) if backup is not None else None,
                "calibration_cert_ids": cert_ids,
                "reading_seqs": sorted(r["seq"] for r in self._reading_chain(attempt)),
                "judge_confirmation_seqs": [c["seq"] for c in attempt["confirmations"]],
                "appeal_window_closed_at": race["appeal_window_closed_at"],
            })
            record["status"] = "ratified"
            record["ratified_at"] = at
            record["technical_delegate"] = technical_delegate
            record["ratification_seq"] = event["seq"]
            attempt["status"] = "纪录已核准"
            return {"record_id": proposal_id, "status": "ratified",
                    "ratified_at": at}

    def reject_record(self, proposal_id, *, at, reason):
        with self._lock:
            record = self._records.get(proposal_id)
            if record is None:
                raise DomainError(f"纪录提案不存在: {proposal_id}", code="missing")
            if record["status"] == "ratified":
                raise DomainError("已核准纪录不可驳回", code="conflict")
            self._append("record-rejected", at,
                         {"proposal_id": proposal_id, "reason": reason})
            record["status"] = "rejected"
            record["reject_reason"] = reason
            return {"proposal_id": proposal_id, "status": "rejected"}

    # ------------------------------------------------------------------ 读模型

    def instant_view(self, attempt_id):
        """即时成绩：只反映原始读数的接收现场，不反映更正/正式判定。"""
        with self._lock:
            attempt = self._require_attempt(attempt_id)
            chain = self._reading_chain(attempt)
            channels = {}
            for ch in CHANNELS:
                rows = attempt["channel_readings"][ch]
                channels[ch] = {
                    "connected": bool(rows),
                    "readings": [self._reading_json(r) for r in
                                 sorted(rows, key=lambda r: (r["occurred_at"], r["seq"]))],
                    "finish": self._raw_finish(attempt, ch),
                }
                channels[ch]["finish"] = (float(channels[ch]["finish"])
                                          if channels[ch]["finish"] is not None else None)
            reaction = [r for r in attempt["referee_readings"]
                        if r["reading_type"] == "reaction"]
            return {
                "model": "instant", "state": attempt["status"],
                "attempt_id": attempt_id, "round_id": attempt["round_id"],
                "race_id": attempt["race_id"], "team_id": attempt["team_id"],
                "climber_order": attempt["climber_order"],
                "reaction": float(reaction[-1]["value"]) if reaction else None,
                "channels": channels,
                "missing_sensors": [dict(m) for m in attempt["missing"]],
                "sealed_order": [{"seq": r["seq"], "source": r["source"],
                                  "reading_id": r["reading_id"],
                                  "reading_type": r["reading_type"],
                                  "occurred_at": r["occurred_at"]} for r in chain],
                "received_count": len(chain),
                "disqualified": any(d["active"] for d in attempt["disqualifications"]),
                "superseded": attempt["superseded"],
                "note": "即时成绩不作为正式赛果或纪录依据",
            }

    def official_view(self, race_id):
        """正式赛果：按场次汇总，含更正轨迹、犯规与申诉状态。"""
        with self._lock:
            race = self._require_race(race_id)
            rows = []
            for aid in race["attempts"]:
                attempt = self._attempts[aid]
                finish, corrected = self._official_finish(attempt)
                rows.append({
                    "attempt_id": aid, "team_id": attempt["team_id"],
                    "climber_order": attempt["climber_order"],
                    "official_time": float(finish) if finish is not None else None,
                    "corrected": corrected,
                    "disqualified": any(d["active"] for d in attempt["disqualifications"]),
                    "superseded": attempt["superseded"],
                    "state": attempt["status"], "finalized_at": attempt["finalized_at"],
                })
            ranked = sorted(
                [r for r in rows if not r["disqualified"] and not r["superseded"]
                 and r["official_time"] is not None],
                key=lambda r: r["official_time"])
            for rank, row in enumerate(ranked, 1):
                row["rank"] = rank
            appeals = [self._appeal_json(self._appeals[a]) for a in race["appeals"]]
            if any(a["outcome"] is None for a in appeals):
                state = "申诉中"
            elif any(r["state"] == "纪录已核准" for r in rows):
                state = "纪录已核准"
            elif any(r["state"] == "正式赛果" for r in rows):
                state = "正式赛果"
            elif any(r["state"] == "待核验" for r in rows):
                state = "待核验"
            else:
                state = "即时成绩"
            return {
                "model": "official", "race_id": race_id,
                "round_id": race["round_id"], "state": state,
                "results": rows, "rankings": ranked,
                "reruns": [dict(r) for r in race["reruns"]],
                "appeals": appeals,
                "appeal_window_closed_at": race["appeal_window_closed_at"],
            }

    def record_view(self, record_id):
        """已核准纪录档案：技术代表据一条纪录即可回溯全部证据。"""
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                raise DomainError(f"纪录不存在: {record_id}", code="missing")
            attempt = self._require_attempt(record["attempt_id"])
            race = self._races[attempt["race_id"]]
            _, cert_ids, devices = self._calibration_issues(attempt)
            # 档案里的证书按“尝试发生时”快照取值，撤销状态也如实展示。
            cert_snapshot = []
            for cert in self._calibrations.values():
                if cert["device_id"] in {d for d, _ in devices} and \
                        cert["valid_from"] <= attempt["at"] and \
                        (cert["valid_to"] is None or attempt["at"] < cert["valid_to"]):
                    cert_snapshot.append(self._cert_json(cert))
            proposal_events = [self._event_json(e) for e in self._events
                               if e["event"].startswith("record-")
                               and e["data"].get("proposal_id") == record_id]
            return {
                "model": "record", "record_id": record_id,
                "status": record["status"],
                "round_id": attempt["round_id"], "race_id": attempt["race_id"],
                "attempt_id": attempt["id"], "team_id": attempt["team_id"],
                "climber_order": attempt["climber_order"],
                "finish_time": record["finish_time"],
                "comparison": record["comparison"],
                "rule_version": record["rule_version"],
                "ratified_at": record.get("ratified_at"),
                "technical_delegate": record.get("technical_delegate"),
                "calibration_certificates": cert_snapshot,
                "raw_readings": [self._reading_json(r)
                                 for r in self._reading_chain(attempt)],
                "missing_sensors": [
                    {**{k: v for k, v in m.items()},
                     "adjudication": attempt["adjudicated_missing"].get(
                         f"{m['reading_type']}:{m['channel']}")}
                    for m in attempt["missing"]],
                "judge_confirmations": [dict(c) for c in attempt["confirmations"]],
                "disqualifications": [dict(d) for d in attempt["disqualifications"]],
                "corrections": [{"seq": c["seq"], "previous_finish": float(c["previous_value"]),
                                 "corrected_finish": float(c["new_value"]),
                                 "reason": c["reason"], "official_id": c["official_id"],
                                 "occurred_at": c["at"]} for c in attempt["corrections"]],
                "supersession": {"superseded": attempt["superseded"],
                                 "reason": attempt["supersede_reason"]},
                "appeals": {
                    "window_closed_at": race["appeal_window_closed_at"],
                    "items": [self._appeal_json(self._appeals[a])
                              for a in race["appeals"]],
                },
                "sealed_event_refs": sorted(
                    {r["seq"] for r in self._reading_chain(attempt)}
                    | {c["seq"] for c in attempt["confirmations"]}
                    | {d["seq"] for d in attempt["disqualifications"]}
                    | {d.get("rescind_seq") for d in attempt["disqualifications"]
                       if d.get("rescind_seq")}
                    | {c["seq"] for c in attempt["corrections"]}),
                "record_events": proposal_events,
            }

    def list_records(self, status=None):
        with self._lock:
            items = [{"record_id": r["id"], "status": r["status"],
                      "attempt_id": r["attempt_id"], "finish_time": r["finish_time"],
                      "comparison": r["comparison"],
                      "rule_version": r["rule_version"],
                      "ratified_at": r.get("ratified_at")}
                     for r in self._records.values()]
            if status:
                items = [r for r in items if r["status"] == status]
            return items

    def events(self, from_seq=0):
        with self._lock:
            return [self._event_json(e) for e in self._events
                    if e["seq"] >= from_seq]

    # --------------------------------------------------------------- 序列化

    @staticmethod
    def _reading_json(r):
        return {k: r[k] for k in (
            "seq", "source", "channel", "device_id", "reading_id", "reading_type",
            "segment", "value", "occurred_at", "received_at") if k in r}

    @staticmethod
    def _cert_json(c):
        return {"cert_id": c["cert_id"], "device_id": c["device_id"],
                "channel": c["channel"], "valid_from": float(c["valid_from"]),
                "valid_to": float(c["valid_to"]) if c["valid_to"] is not None else None,
                "revoked": c["revoked"], "revoke_reason": c.get("revoke_reason"),
                "registered_seq": c["registered_seq"],
                "revoked_seq": c.get("revoked_seq"), "note": c.get("note")}

    @staticmethod
    def _appeal_json(a):
        return {"appeal_id": a["id"], "filed_by": a["filed_by"],
                "grounds": a["grounds"], "filed_at": a["filed_at"],
                "outcome": a["outcome"], "resolved_at": a["resolved_at"],
                "note": a["note"]}

    def _event_json(self, e):
        return {"seq": e["seq"], "event": e["event"],
                "occurred_at": e["occurred_at"], "data": dict(e["data"])}
