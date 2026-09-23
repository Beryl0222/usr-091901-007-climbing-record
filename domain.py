"""速度接力纪录核准领域服务。

所有命令都封存为只追加事件（见 eventlog.SealedLog），读模型由事件重放得到：
- 即时成绩 live_result：最近原始读数，未核对；
- 正式赛果 round_results：裁判确认并正式化的轮次结果；
- 已核准纪录 list_records / record_evidence：技术代表签发，附完整证据包。

核心规则（与 domain_contract.json 不变量对应）：
- 读数幂等去重、设备序号递增由封存日志保证；领域层保证每通道每段仅一条；
- 传感器缺失只登记 sensor_missing_marked，尝试进入“待核验”，绝不补值；
- 换人、重赛、犯规撤销、成绩更正均为新事件并链接被修订事件；
- 纪录标准/精度规则按生效时间版本化，仅适用于生效后的尝试；
- 核准逐项核对棒次、分段、起跑反应、校准覆盖、裁判确认与申诉状态。
"""

import functools
import threading
from datetime import datetime, timedelta

from eventlog import SealedLog, SealedLogError

# 速度接力（两名选手）原始检查点：一号起跑反应、交接点、二号反应、终点
CHECKPOINTS = ("reaction1", "split", "reaction2", "finish")
CHANNELS = ("primary", "backup")
ROUND_CODES = ("heat", "quarterfinal", "semifinal", "final")
ROUND_NAMES = {
    "heat": "预赛", "quarterfinal": "四分之一决赛",
    "semifinal": "半决赛", "final": "决赛",
}
DEFAULT_PROTEST_WINDOW_SECONDS = 900


class DomainError(Exception):
    """领域规则拒绝该命令。"""


def _parse_ts(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise DomainError(f"无法解析时间：{value!r}")


def _require(condition, message):
    if not condition:
        raise DomainError(message)


def idempotent(command):
    """命令入口先查幂等键：网络重传直接返回首次封存的事件，不再做业务校验。"""
    @functools.wraps(command)
    def wrapper(self, *args, **kwargs):
        key = kwargs.get("idem_key")
        if key is not None:
            prior = self.log.by_idem(key)
            if prior is not None:
                return prior, True
        return command(self, *args, **kwargs)
    return wrapper


class RecordService:
    """命令封存 + 事件重放投影的领域服务。"""

    def __init__(self, log=None, *, log_path=None):
        self._service_lock = threading.RLock()
        self.log = log or SealedLog(log_path)
        self._reset_projections()
        for event in self.log.events():
            self._apply(event)

    def _reset_projections(self):
        self.meetings = {}
        self.rounds = {}        # round_id -> round
        self.teams = {}         # team_id -> team
        self.attempts = {}      # attempt_id -> attempt
        self.calibrations = {}  # (device_id, channel, cert_no) -> cert
        self.standards = []     # 纪录标准版本（按封存顺序）
        self.precision_rules = []
        self.protests = {}
        self.records = {}       # record_id -> 已核准纪录
        self._records_by_attempt = {}

    # ======================================================================
    # 赛事管理
    # ======================================================================

    @idempotent
    def register_meeting(self, meeting_id, name, date, *, ts,
                         device_id="jury-terminal", idem_key=None,
                         protest_window_seconds=DEFAULT_PROTEST_WINDOW_SECONDS):
        _require(meeting_id not in self.meetings, f"赛事已存在：{meeting_id}")
        return self._seal("meeting_registered", {
            "meeting_id": meeting_id, "name": name, "date": date,
            "protest_window_seconds": protest_window_seconds,
        }, ts=ts, device_id=device_id, idem_key=idem_key)

    @idempotent
    def schedule_round(self, meeting_id, round_id, code, *, heat_no=None,
                       ts, device_id="jury-terminal", idem_key=None):
        _require(meeting_id in self.meetings, f"赛事不存在：{meeting_id}")
        _require(code in ROUND_CODES, f"未知轮次：{code}")
        _require(round_id not in self.rounds, f"轮次已存在：{round_id}")
        return self._seal("round_scheduled", {
            "meeting_id": meeting_id, "round_id": round_id,
            "code": code, "name": ROUND_NAMES[code], "heat_no": heat_no,
        }, ts=ts, device_id=device_id, idem_key=idem_key)

    @idempotent
    def register_team(self, meeting_id, team_id, name, athlete_ids, *, ts,
                      device_id="jury-terminal", idem_key=None):
        _require(meeting_id in self.meetings, f"赛事不存在：{meeting_id}")
        _require(team_id not in self.teams, f"队伍已存在：{team_id}")
        athlete_ids = list(athlete_ids)
        _require(len(athlete_ids) >= 2, "速度接力至少需要登记两名选手")
        return self._seal("team_roster_set", {
            "meeting_id": meeting_id, "team_id": team_id, "name": name,
            "athlete_ids": athlete_ids,
        }, ts=ts, device_id=device_id, idem_key=idem_key)

    @idempotent
    def submit_lineup(self, team_id, round_id, athlete_ids, *, ts,
                      device_id="jury-terminal", idem_key=None):
        team, round_ = self._team_round(team_id, round_id)
        athlete_ids = list(athlete_ids)
        _require(len(athlete_ids) == 2, "接力棒次必须恰好为两名选手")
        _require(all(a in team["roster"] for a in athlete_ids),
                 "棒次选手必须来自队伍报名名单")
        _require(round_id not in team["lineups"], "该轮次已提交棒次，换人请使用 lineup_changed")
        return self._seal("team_lineup_submitted", {
            "team_id": team_id, "round_id": round_id,
            "meeting_id": round_["meeting_id"],
            "athlete_ids": athlete_ids,
        }, ts=ts, device_id=device_id, idem_key=idem_key)

    @idempotent
    def change_lineup(self, team_id, round_id, athlete_ids, reason, *, ts,
                      device_id="jury-terminal", idem_key=None):
        team, round_ = self._team_round(team_id, round_id)
        athlete_ids = list(athlete_ids)
        _require(len(athlete_ids) == 2, "接力棒次必须恰好为两名选手")
        _require(all(a in team["roster"] for a in athlete_ids),
                 "替换选手必须来自队伍报名名单")
        _require(round_id in team["lineups"], "该轮次尚未提交棒次，无需换人")
        previous = team["lineups"][round_id]
        return self._seal("lineup_changed", {
            "team_id": team_id, "round_id": round_id,
            "meeting_id": round_["meeting_id"],
            "previous_athlete_ids": previous,
            "athlete_ids": athlete_ids,
            "reason": reason,
            "linked_event_seq": team["lineup_events"][round_id],
        }, ts=ts, device_id=device_id, idem_key=idem_key)

    # ======================================================================
    # 设备检定 / 规则版本
    # ======================================================================

    @idempotent
    def register_calibration(self, cert_no, lane, device_id, channel, *,
                             verified_by, issued_at, valid_from, valid_until,
                             digest, ts, idem_key=None):
        _require(channel in CHANNELS, f"未知计时通道：{channel}")
        key = (device_id, channel, cert_no)
        _require(key not in self.calibrations, f"校准证书已登记：{cert_no}")
        _require(_parse_ts(valid_from) <= _parse_ts(valid_until), "校准有效期倒置")
        return self._seal("calibration_registered", {
            "cert_no": cert_no, "lane": lane, "device_id": device_id,
            "channel": channel, "verified_by": verified_by,
            "issued_at": issued_at, "valid_from": valid_from,
            "valid_until": valid_until, "digest": digest,
        }, ts=ts, device_id="calibration-terminal", idem_key=idem_key)

    @idempotent
    def issue_record_standard(self, rule_id, event_key, threshold_seconds, *,
                              effective_from, issued_by, ts, idem_key=None):
        _require(threshold_seconds > 0, "纪录标准必须为正数")
        _require(not any(r["rule_id"] == rule_id for r in self.standards),
                 f"标准编号重复：{rule_id}")
        return self._seal("record_standard_issued", {
            "rule_id": rule_id, "event_key": event_key,
            "threshold_seconds": threshold_seconds,
            "effective_from": effective_from, "issued_by": issued_by,
        }, ts=ts, device_id="jury-terminal", idem_key=idem_key)

    @idempotent
    def issue_precision_rule(self, rule_id, *, channel_tolerance_seconds,
                             false_start_threshold_seconds, timing_resolution,
                             effective_from, issued_by, ts, idem_key=None):
        _require(channel_tolerance_seconds >= 0, "双通道容差不能为负")
        _require(false_start_threshold_seconds >= 0, "抢跑阈值不能为负")
        _require(not any(r["rule_id"] == rule_id for r in self.precision_rules),
                 f"精度规则编号重复：{rule_id}")
        return self._seal("precision_rule_issued", {
            "rule_id": rule_id,
            "channel_tolerance_seconds": channel_tolerance_seconds,
            "false_start_threshold_seconds": false_start_threshold_seconds,
            "timing_resolution": timing_resolution,
            "effective_from": effective_from, "issued_by": issued_by,
        }, ts=ts, device_id="jury-terminal", idem_key=idem_key)

    # ======================================================================
    # 尝试与原始读数
    # ======================================================================

    @idempotent
    def start_attempt(self, attempt_id, meeting_id, round_id, team_id, lane, *,
                      ts, device_id="jury-terminal", idem_key=None,
                      supersedes=None):
        _require(meeting_id in self.meetings, f"赛事不存在：{meeting_id}")
        round_ = self.rounds.get(round_id)
        _require(round_ is not None and round_["meeting_id"] == meeting_id,
                 f"轮次不属于该赛事：{round_id}")
        team = self.teams.get(team_id)
        _require(team is not None and team["meeting_id"] == meeting_id,
                 f"队伍不属于该赛事：{team_id}")
        _require(attempt_id not in self.attempts, f"尝试已存在：{attempt_id}")
        lineup = team["lineups"].get(round_id)
        _require(lineup is not None, "该队本轮尚未提交棒次")
        if supersedes is not None:
            prior = self.attempts.get(supersedes)
            _require(prior is not None, f"被重赛尝试不存在：{supersedes}")
            _require(prior["status"] == "rerun", "只有获准重赛的尝试可被新尝试接替")
        return self._seal("attempt_started", {
            "attempt_id": attempt_id, "meeting_id": meeting_id,
            "round_id": round_id, "team_id": team_id, "lane": lane,
            "climbers": list(lineup), "supersedes": supersedes,
        }, ts=ts, device_id=device_id, idem_key=idem_key)

    @idempotent
    def ingest_reading(self, attempt_id, phase, channel, value, *, ts,
                       device_id, channel_seq, idem_key):
        """封存一条原始计时读数。网络重传由 idem_key 在日志层去重。"""
        attempt = self._attempt(attempt_id)
        _require(phase in CHECKPOINTS, f"未知计时检查点：{phase}")
        _require(channel in CHANNELS, f"未知计时通道：{channel}")
        _require(isinstance(value, (int, float)) and value >= 0,
                 "读数必须为非负秒数")
        _require(attempt["status"] in ("recorded", "false_start"),
                 f"尝试状态 {attempt['status']} 不再接受读数")
        existing = attempt["readings"].get(phase, {}).get(channel)
        if existing is not None:
            raise DomainError(
                f"{phase}/{channel} 读数已封存（事件 {existing['event_seq']}），原始读数不可覆盖")
        _require(attempt["referee"] is None,
                 "裁判已确认该尝试，不能再追加读数；如需更改请走更正或重赛")
        try:
            return self._seal("timer_reading_ingested", {
                "attempt_id": attempt_id, "phase": phase, "channel": channel,
                "value": value, "occurred_ts": ts,
            }, ts=ts, device_id=device_id, idem_key=idem_key,
                channel_seq=channel_seq)
        except SealedLogError as exc:
            raise DomainError(str(exc))

    @idempotent
    def mark_sensor_missing(self, attempt_id, phase, channel, reason, *, ts,
                            device_id, channel_seq, idem_key):
        """传感器缺失：只登记待核，绝不写入推断值。"""
        attempt = self._attempt(attempt_id)
        _require(phase in CHECKPOINTS, f"未知计时检查点：{phase}")
        _require(channel in CHANNELS, f"未知计时通道：{channel}")
        _require(attempt["readings"].get(phase, {}).get(channel) is None,
                 "该检查点已有读数，不能标记缺失")
        _require((phase, channel) not in attempt["missing"],
                 "该传感器缺失已登记")
        _require(attempt["referee"] is None,
                 "裁判已确认该尝试，不能再追加缺感标记")
        try:
            return self._seal("sensor_missing_marked", {
                "attempt_id": attempt_id, "phase": phase, "channel": channel,
                "reason": reason, "occurred_ts": ts,
            }, ts=ts, device_id=device_id, idem_key=idem_key,
                channel_seq=channel_seq)
        except SealedLogError as exc:
            raise DomainError(str(exc))

    # ======================================================================
    # 裁判终端：确认 / 犯规 / 重赛 / 申诉 / 更正 / 正式化
    # ======================================================================

    @idempotent
    def referee_confirm(self, attempt_id, *, by, ts, note="", idem_key=None):
        attempt = self._attempt(attempt_id)
        _require(attempt["status"] in ("recorded",),
                 f"尝试状态 {attempt['status']} 无法确认")
        return self._seal("referee_confirmation", {
            "attempt_id": attempt_id, "by": by, "note": note,
            "climbers": list(attempt["climbers"]),
        }, ts=ts, device_id="referee-terminal", idem_key=idem_key)

    @idempotent
    def declare_false_start(self, attempt_id, leg, reaction_seconds, *, by, ts,
                            note="", idem_key=None):
        attempt = self._attempt(attempt_id)
        _require(leg in (1, 2), "棒次必须为 1 或 2")
        _require(isinstance(reaction_seconds, (int, float)), "起跑反应必须为秒数")
        _require(attempt["status"] == "recorded",
                 f"尝试状态 {attempt['status']} 不能判罚抢跑")
        return self._seal("false_start_declared", {
            "attempt_id": attempt_id, "leg": leg,
            "reaction_seconds": reaction_seconds,
            "by": by, "note": note,
        }, ts=ts, device_id="referee-terminal", idem_key=idem_key)

    @idempotent
    def revoke_false_start(self, attempt_id, *, by, ts, reason, idem_key=None):
        """犯规撤销：判罚事件保留，另起撤销事件链接原判罚。"""
        attempt = self._attempt(attempt_id)
        _require(attempt["status"] == "false_start", "该尝试没有可撤销的抢跑判罚")
        return self._seal("false_start_revoked", {
            "attempt_id": attempt_id, "by": by, "reason": reason,
            "linked_event_seq": attempt["false_start"]["event_seq"],
        }, ts=ts, device_id="referee-terminal", idem_key=idem_key)

    @idempotent
    def grant_rerun(self, attempt_id, *, by, ts, reason, idem_key=None):
        attempt = self._attempt(attempt_id)
        _require(attempt["status"] in ("recorded", "false_start"),
                 f"尝试状态 {attempt['status']} 不允许重赛")
        return self._seal("rerun_granted", {
            "attempt_id": attempt_id, "by": by, "reason": reason,
            "previous_status": attempt["status"],
        }, ts=ts, device_id="referee-terminal", idem_key=idem_key)

    @idempotent
    def void_rerun(self, attempt_id, *, by, ts, reason, idem_key=None):
        """重赛准许被撤销：链接原准许事件，恢复尝试此前状态。"""
        attempt = self._attempt(attempt_id)
        _require(attempt["status"] == "rerun", "该尝试未处于重赛状态")
        return self._seal("rerun_voided", {
            "attempt_id": attempt_id, "by": by, "reason": reason,
            "restored_status": attempt["pre_rerun_status"],
            "linked_event_seq": attempt["rerun_event_seq"],
        }, ts=ts, device_id="referee-terminal", idem_key=idem_key)

    @idempotent
    def file_protest(self, protest_id, meeting_id, round_id, attempt_id,
                     team_id, reason, *, ts, idem_key=None):
        attempt = self._attempt(attempt_id)
        _require(attempt["meeting_id"] == meeting_id
                 and attempt["round_id"] == round_id, "申诉对象与赛事/轮次不符")
        _require(protest_id not in self.protests, f"申诉已存在：{protest_id}")
        return self._seal("protest_filed", {
            "protest_id": protest_id, "meeting_id": meeting_id,
            "round_id": round_id, "attempt_id": attempt_id,
            "team_id": team_id, "reason": reason,
        }, ts=ts, device_id="protest-terminal", idem_key=idem_key)

    @idempotent
    def resolve_protest(self, protest_id, *, verdict, by, ts, note="",
                        idem_key=None):
        protest = self.protests.get(protest_id)
        _require(protest is not None, f"申诉不存在：{protest_id}")
        _require(protest["status"] == "open", "该申诉已裁决")
        _require(verdict in ("upheld", "rejected"), "裁决必须为 upheld/rejected")
        return self._seal("protest_resolved", {
            "protest_id": protest_id, "verdict": verdict, "by": by,
            "note": note, "linked_event_seq": protest["event_seq"],
        }, ts=ts, device_id="jury-terminal", idem_key=idem_key)

    @idempotent
    def correct_result(self, attempt_id, *, by, ts, reason,
                       corrected_total=None, corrected_checkpoints=None,
                       idem_key=None):
        """成绩更正：原始读数保留不动，更正另存并链接上一版本。"""
        attempt = self._attempt(attempt_id)
        _require(attempt["status"] == "recorded",
                 f"尝试状态 {attempt['status']} 不能更正成绩")
        if corrected_checkpoints is not None:
            _require(isinstance(corrected_checkpoints, dict), "更正值必须按检查点给出")
            _require(all(p in CHECKPOINTS for p in corrected_checkpoints),
                     "更正包含未知检查点")
        if corrected_total is not None:
            _require(isinstance(corrected_total, (int, float))
                     and corrected_total >= 0, "更正总成绩必须为非负秒数")
        linked = attempt["last_correction_seq"] or attempt["official_seq"]
        return self._seal("result_corrected", {
            "attempt_id": attempt_id, "by": by, "reason": reason,
            "corrected_total": corrected_total,
            "corrected_checkpoints": corrected_checkpoints,
            "linked_event_seq": linked,
        }, ts=ts, device_id="referee-terminal", idem_key=idem_key)

    def officialize_round(self, round_id, *, by, ts, idem_key=None):
        """将轮次中满足条件的尝试正式化为赛果；待核尝试列出但不正式化。"""
        round_ = self.rounds.get(round_id)
        _require(round_ is not None, f"轮次不存在：{round_id}")
        ready, held = [], []
        for aid in round_["attempts"]:
            attempt = self.attempts[aid]
            if attempt["status"] in ("voided", "rerun"):
                continue
            pending, reasons = self._verification_state(attempt)
            confirmed = self._confirmation_current(attempt)
            if attempt["status"] == "recorded" and not pending and confirmed:
                ready.append(aid)
            else:
                held.append({"attempt_id": aid, "status": attempt["status"],
                             "pending_reasons": reasons,
                             "referee_confirmed": bool(confirmed)})
        _require(ready, "没有可正式化的尝试（全部待核、未确认或已失效）")
        return self._seal("result_officialized", {
            "round_id": round_id, "by": by,
            "attempt_ids": ready, "held_back": held,
        }, ts=ts, device_id="jury-terminal", idem_key=idem_key)

    # ======================================================================
    # 纪录核准（技术代表）
    # ======================================================================

    @idempotent
    def ratify_record(self, attempt_id, record_id, *, event_key, by, ts,
                      idem_key=None):
        """逐项核对签发清单，全部通过才封存 record_ratified。"""
        attempt = self._attempt(attempt_id)
        failures = self._ratification_failures(attempt, event_key, ts)
        _require(not failures, "；".join(failures))
        race_ts = self._race_ts(attempt)
        standard = self._active_rule(self.standards, race_ts)
        precision = self._active_rule(self.precision_rules, race_ts)
        evidence = self._evidence_bundle(attempt, race_ts, standard, precision)
        official_time = self._official_time(attempt)
        return self._seal("record_ratified", {
            "record_id": record_id, "attempt_id": attempt_id,
            "meeting_id": attempt["meeting_id"], "round_id": attempt["round_id"],
            "team_id": attempt["team_id"], "event_key": event_key,
            "official_time_seconds": official_time,
            "standard_rule_id": standard["rule_id"],
            "precision_rule_id": precision["rule_id"],
            "ratified_by": by, "evidence": evidence,
        }, ts=ts, device_id="technical-delegate", idem_key=idem_key)

    @idempotent
    def reject_record(self, attempt_id, *, by, ts, reason, idem_key=None):
        attempt = self._attempt(attempt_id)
        _require(attempt.get("record") is None, "该尝试已有核准结论")
        return self._seal("record_rejected", {
            "attempt_id": attempt_id, "by": by, "reason": reason,
        }, ts=ts, device_id="technical-delegate", idem_key=idem_key)

    def _ratification_failures(self, attempt, event_key, ratify_ts):
        failures = []
        ratify_ts = _parse_ts(ratify_ts)
        round_ = self.rounds[attempt["round_id"]]
        if round_["code"] != "final":
            failures.append("只有决赛轮次可核准纪录")
        if attempt["status"] != "recorded":
            failures.append(f"尝试状态为 {attempt['status']}，非有效成绩")
        if not attempt["official_ts"]:
            failures.append("成绩尚未正式化")
        if not self._confirmation_current(attempt):
            failures.append("裁判确认缺失或确认后又发生更正")
        if len(attempt["climbers"]) != 2:
            failures.append("接力棒次不是两名选手")

        race_ts = self._race_ts(attempt)
        precision = self._active_rule(self.precision_rules, race_ts)
        if precision is None:
            failures.append("尝试发生时没有生效的精度规则")
        standard = self._active_rule(self.standards, race_ts)
        if standard is None:
            failures.append("尝试发生时没有生效的纪录标准")
        elif standard["event_key"] != event_key:
            failures.append(f"赛事 {event_key} 没有对应的生效纪录标准")

        pending, reasons = self._verification_state(attempt)
        if pending:
            failures.extend(reasons)

        # 起跑反应合法（以一号选手出发为准，对照生效精度规则的抢跑阈值）
        reaction = self._checkpoint_value(attempt, "reaction1")
        if precision is not None:
            if reaction is None:
                failures.append("缺少起跑反应时间")
            elif reaction < precision["false_start_threshold_seconds"]:
                failures.append(
                    f"起跑反应 {reaction}s 早于抢跑阈值 "
                    f"{precision['false_start_threshold_seconds']}s")

        # 校准证书覆盖比赛时刻、赛道与设备通道
        if precision is not None and not pending:
            for phase in CHECKPOINTS:
                for channel in CHANNELS:
                    reading = attempt["readings"].get(phase, {}).get(channel)
                    cert = self._covering_cert(reading["device_id"], channel,
                                               attempt["lane"], race_ts)
                    if cert is None:
                        failures.append(
                            f"{phase}/{channel} 设备在比赛时刻无有效校准证书")

        # 申诉窗口与裁决：窗口未过一律等待（驳回本次申诉不等于截止，
        # 窗口期内仍可能有新申诉）；窗口过后若有成立裁决同样不准核准。
        protest_info = self._protest_status(attempt, ratify_ts)
        if protest_info["open"]:
            failures.append("存在尚未裁决的申诉")
        if any(pr["status"] == "upheld" for pr in protest_info["protests"]):
            failures.append("申诉曾被裁定成立，成绩须经更正或重赛并重新确认")
        if ratify_ts <= _parse_ts(protest_info["deadline_ts"]):
            failures.append("申诉截止时间未到，不能提前核准")

        # 成绩达到生效标准
        if standard is not None and standard["event_key"] == event_key \
                and not pending and attempt["status"] == "recorded":
            official_time = self._official_time(attempt)
            if official_time is not None and \
                    official_time > standard["threshold_seconds"] + 1e-12:
                failures.append(
                    f"成绩 {official_time}s 未达到生效标准 "
                    f"{standard['threshold_seconds']}s")

        # 重赛血缘：被接替的旧尝试不能再核准
        if attempt["superseded_by"]:
            failures.append("该尝试已被重赛新尝试接替")
        return failures

    # ======================================================================
    # 读模型
    # ======================================================================

    def live_result(self, attempt_id):
        """即时成绩：最近一次原始读数；缺感时状态为待核验。"""
        attempt = self._attempt(attempt_id)
        pending, reasons = self._verification_state(attempt)
        latest = None
        for phase in CHECKPOINTS:
            for channel in CHANNELS:
                reading = attempt["readings"].get(phase, {}).get(channel)
                if reading and (latest is None
                                or reading["event_seq"] > latest["event_seq"]):
                    latest = reading
        finish = self._checkpoint_value(attempt, "finish")
        split = self._checkpoint_value(attempt, "split")
        return {
            "published": "live",
            "state": "待核验" if pending else "即时成绩",
            "attempt_id": attempt_id,
            "round_id": attempt["round_id"], "team_id": attempt["team_id"],
            "lane": attempt["lane"], "climbers": list(attempt["climbers"]),
            "latest_reading": None if latest is None else {
                "phase": latest["phase"], "channel": latest["channel"],
                "value": latest["value"], "occurred_ts": latest["occurred_ts"],
                "event_seq": latest["event_seq"],
            },
            "provisional_total": finish,
            "provisional_legs": self._derived_legs(attempt),
            "available_channels": {
                phase: sorted(attempt["readings"].get(phase, {}).keys())
                for phase in CHECKPOINTS
            },
            "missing_sensors": [dict(m) for m in attempt["missing"]],
            "pending_reasons": reasons,
            "raw_read_only": True,
        }

    def round_results(self, round_id):
        """正式赛果：只在轮次正式化之后发布。"""
        round_ = self.rounds.get(round_id)
        _require(round_ is not None, f"轮次不存在：{round_id}")
        _require(round_["official_ts"], "该轮次尚未正式化，无正式赛果")
        rows = []
        for aid in round_["attempts"]:
            attempt = self.attempts[aid]
            if attempt["status"] in ("voided", "rerun"):
                continue
            rows.append({
                "attempt_id": aid, "team_id": attempt["team_id"],
                "lane": attempt["lane"], "climbers": list(attempt["climbers"]),
                "status": attempt["status"],
                "official_time_seconds": self._official_time(attempt)
                    if attempt["status"] == "recorded" else None,
                "corrected": bool(attempt["corrections"]),
                "false_start": None if not attempt["false_start"] else {
                    "leg": attempt["false_start"]["leg"],
                    "reaction_seconds": attempt["false_start"]["reaction"],
                    "revoked": attempt["false_start"]["revoked"],
                },
            })
        timed = sorted((r for r in rows if r["official_time_seconds"] is not None),
                       key=lambda r: r["official_time_seconds"])
        for rank, row in enumerate(timed, start=1):
            row["rank"] = rank
        others = [r for r in rows if r["official_time_seconds"] is None]
        for row in others:
            row["rank"] = None
        return {
            "published": "official",
            "state": "正式赛果",
            "round_id": round_id, "meeting_id": round_["meeting_id"],
            "round_code": round_["code"], "round_name": round_["name"],
            "officialized_ts": round_["official_ts"],
            "results": timed + others,
        }

    def list_records(self):
        """已核准纪录一览。"""
        return {
            "published": "ratified",
            "state": "纪录已核准",
            "records": [
                {
                    "record_id": rec["record_id"],
                    "attempt_id": rec["attempt_id"],
                    "meeting_id": rec["meeting_id"],
                    "round_id": rec["round_id"],
                    "team_id": rec["team_id"],
                    "event_key": rec["event_key"],
                    "official_time_seconds": rec["official_time"],
                    "ratified_ts": rec["ratified_ts"],
                    "ratified_by": rec["ratified_by"],
                }
                for rec in sorted(self.records.values(),
                                  key=lambda r: r["ratified_seq"])
            ],
        }

    def record_evidence(self, *, record_id=None, attempt_id=None,
                        time_seconds=None):
        """从一条已核准纪录取出完整证据包。

        支持按 record_id、attempt_id 或成绩秒数（如 9.58）定位。
        """
        rec = self._locate_record(record_id, attempt_id, time_seconds)
        attempt = self.attempts[rec["attempt_id"]]
        race_ts = self._race_ts(attempt)
        standard = self._active_rule(self.standards, race_ts)
        precision = self._active_rule(self.precision_rules, race_ts)
        bundle = self._evidence_bundle(attempt, race_ts, standard, precision)
        return {
            "published": "ratified",
            "state": "纪录已核准",
            "record": {
                "record_id": rec["record_id"],
                "attempt_id": rec["attempt_id"],
                "meeting_id": rec["meeting_id"],
                "round_id": rec["round_id"],
                "team_id": rec["team_id"],
                "event_key": rec["event_key"],
                "official_time_seconds": rec["official_time"],
                "ratified_ts": rec["ratified_ts"],
                "ratified_by": rec["ratified_by"],
                "ratified_event_seq": rec["ratified_seq"],
                "standard_rule_id": rec["standard_rule_id"],
                "precision_rule_id": rec["precision_rule_id"],
            },
            **bundle,
        }

    def _evidence_bundle(self, attempt, race_ts, standard, precision):
        raw_segments = []
        for phase in CHECKPOINTS:
            for channel in CHANNELS:
                reading = attempt["readings"].get(phase, {}).get(channel)
                if reading:
                    raw_segments.append({
                        "phase": phase, "channel": channel,
                        "value_seconds": reading["value"],
                        "occurred_ts": reading["occurred_ts"],
                        "device_id": reading["device_id"],
                        "channel_seq": reading["channel_seq"],
                        "event_seq": reading["event_seq"],
                        "idem_key": reading.get("idem_key"),
                        "cert_no": (self._covering_cert(
                            reading["device_id"], channel,
                            attempt["lane"], race_ts) or {}).get("cert_no"),
                    })
        certs = {}
        for segment in raw_segments:
            if segment["cert_no"]:
                certs[segment["cert_no"]] = self._cert_view(
                    segment["device_id"], segment["channel"], segment["cert_no"])
        confirmation = None
        if attempt["referee"]:
            confirmation = dict(attempt["referee"])
        chain = []
        cur = attempt
        while cur and cur.get("supersedes"):
            cur = self.attempts[cur["supersedes"]]
            chain.append({"attempt_id": cur["attempt_id"],
                          "status": cur["status"]})
        broken_at = self.log.verify_chain()
        return {
            "climber_order": list(attempt["climbers"]),
            "derived_legs": self._derived_legs(attempt),
            "raw_segments": raw_segments,
            "missing_sensors": [dict(m) for m in attempt["missing"]],
            "calibration_certificates": list(certs.values()),
            "referee_confirmation": confirmation,
            "protest_status": self._protest_status(attempt, None),
            "corrections": [dict(c) for c in attempt["corrections"]],
            "lineage": {
                "supersedes": attempt["supersedes"],
                "superseded_by": attempt["superseded_by"],
                "rerun_chain_oldest_first": chain[::-1],
            },
            "rule_versions_at_race": {
                "record_standard": None if standard is None else {
                    "rule_id": standard["rule_id"],
                    "threshold_seconds": standard["threshold_seconds"],
                    "effective_from": standard["effective_from"],
                },
                "precision_rule": None if precision is None else {
                    "rule_id": precision["rule_id"],
                    "channel_tolerance_seconds":
                        precision["channel_tolerance_seconds"],
                    "false_start_threshold_seconds":
                        precision["false_start_threshold_seconds"],
                    "timing_resolution": precision["timing_resolution"],
                    "effective_from": precision["effective_from"],
                },
                "race_ts": race_ts.isoformat() if race_ts else None,
            },
            "seal": {
                "tail_hash": self.log.events()[-1]["hash"] if self.log.events()
                            else None,
                "chain_verified": broken_at is None,
                "broken_at_seq": broken_at,
            },
        }

    # ======================================================================
    # 事件封存与重放
    # ======================================================================

    def _seal(self, event_type, payload, *, ts, device_id, idem_key,
              channel_seq=None):
        ts_text = ts if isinstance(ts, str) else ts.isoformat()
        try:
            event, duplicated = self.log.append(
                event_type, payload, ts=ts_text, device_id=device_id,
                idem_key=idem_key, channel_seq=channel_seq)
        except SealedLogError as exc:
            raise DomainError(str(exc))
        if not duplicated:
            self._apply(event)
        return event, duplicated

    def _apply(self, event):
        kind = event["type"]
        p = event["payload"]
        handler = getattr(self, f"_apply_{kind}", None)
        if handler:
            handler(event, p)

    def _apply_meeting_registered(self, e, p):
        self.meetings[p["meeting_id"]] = {
            "meeting_id": p["meeting_id"], "name": p["name"], "date": p["date"],
            "protest_window_seconds": p.get(
                "protest_window_seconds", DEFAULT_PROTEST_WINDOW_SECONDS),
            "event_seq": e["seq"],
        }

    def _apply_round_scheduled(self, e, p):
        self.rounds[p["round_id"]] = {
            "round_id": p["round_id"], "meeting_id": p["meeting_id"],
            "code": p["code"], "name": p["name"], "heat_no": p.get("heat_no"),
            "status": "scheduled", "attempts": [],
            "official_ts": None, "official_seq": None,
        }

    def _apply_team_roster_set(self, e, p):
        self.teams[p["team_id"]] = {
            "team_id": p["team_id"], "meeting_id": p["meeting_id"],
            "name": p["name"], "roster": list(p["athlete_ids"]),
            "lineups": {}, "lineup_events": {},
        }

    def _apply_team_lineup_submitted(self, e, p):
        self.teams[p["team_id"]]["lineups"][p["round_id"]] = list(p["athlete_ids"])
        self.teams[p["team_id"]]["lineup_events"][p["round_id"]] = e["seq"]

    def _apply_lineup_changed(self, e, p):
        team = self.teams[p["team_id"]]
        team["lineups"][p["round_id"]] = list(p["athlete_ids"])
        team["lineup_events"][p["round_id"]] = e["seq"]

    def _apply_calibration_registered(self, e, p):
        self.calibrations[(p["device_id"], p["channel"], p["cert_no"])] = dict(p)

    def _apply_record_standard_issued(self, e, p):
        self.standards.append(dict(p, event_seq=e["seq"]))

    def _apply_precision_rule_issued(self, e, p):
        self.precision_rules.append(dict(p, event_seq=e["seq"]))

    def _apply_attempt_started(self, e, p):
        round_ = self.rounds[p["round_id"]]
        attempt = {
            "attempt_id": p["attempt_id"], "meeting_id": p["meeting_id"],
            "round_id": p["round_id"], "team_id": p["team_id"],
            "lane": p["lane"], "climbers": list(p["climbers"]),
            "supersedes": p.get("supersedes"), "superseded_by": None,
            "status": "recorded", "readings": {}, "missing": [],
            "referee": None, "false_start": None,
            "pre_rerun_status": None, "rerun_event_seq": None,
            "corrections": [], "last_correction_seq": None,
            "official_ts": None, "official_seq": None,
            "started_ts": e["ts"], "started_seq": e["seq"], "record": None,
        }
        self.attempts[p["attempt_id"]] = attempt
        round_["attempts"].append(p["attempt_id"])
        round_["status"] = "active"
        if p.get("supersedes"):
            self.attempts[p["supersedes"]]["superseded_by"] = p["attempt_id"]

    def _apply_timer_reading_ingested(self, e, p):
        attempt = self.attempts[p["attempt_id"]]
        attempt["readings"].setdefault(p["phase"], {})[p["channel"]] = {
            "phase": p["phase"], "channel": p["channel"],
            "value": p["value"], "occurred_ts": p["occurred_ts"],
            "device_id": e["device_id"], "channel_seq": e["channel_seq"],
            "idem_key": e.get("idem_key"), "event_seq": e["seq"],
        }

    def _apply_sensor_missing_marked(self, e, p):
        attempt = self.attempts[p["attempt_id"]]
        attempt["missing"].append({
            "phase": p["phase"], "channel": p["channel"],
            "reason": p["reason"], "occurred_ts": p["occurred_ts"],
            "device_id": e["device_id"], "event_seq": e["seq"],
        })

    def _apply_referee_confirmation(self, e, p):
        self.attempts[p["attempt_id"]]["referee"] = {
            "by": p["by"], "note": p.get("note", ""), "ts": e["ts"],
            "event_seq": e["seq"], "confirmed_climbers": list(p["climbers"]),
        }

    def _apply_false_start_declared(self, e, p):
        attempt = self.attempts[p["attempt_id"]]
        attempt["status"] = "false_start"
        attempt["false_start"] = {
            "leg": p["leg"], "reaction": p["reaction_seconds"],
            "by": p["by"], "note": p.get("note", ""), "ts": e["ts"],
            "event_seq": e["seq"], "revoked": False, "revocations": [],
        }

    def _apply_false_start_revoked(self, e, p):
        attempt = self.attempts[p["attempt_id"]]
        attempt["status"] = "recorded"
        attempt["false_start"]["revoked"] = True
        attempt["false_start"]["revocations"].append({
            "by": p["by"], "reason": p["reason"], "ts": e["ts"],
            "event_seq": e["seq"], "linked_event_seq": p["linked_event_seq"],
        })

    def _apply_rerun_granted(self, e, p):
        attempt = self.attempts[p["attempt_id"]]
        attempt["pre_rerun_status"] = p["previous_status"]
        attempt["rerun_event_seq"] = e["seq"]
        attempt["status"] = "rerun"
        attempt["rerun_by"] = p["by"]
        attempt["rerun_reason"] = p["reason"]

    def _apply_rerun_voided(self, e, p):
        attempt = self.attempts[p["attempt_id"]]
        attempt["status"] = p["restored_status"]
        attempt["rerun_voided"] = {
            "by": p["by"], "reason": p["reason"], "ts": e["ts"],
            "event_seq": e["seq"], "linked_event_seq": p["linked_event_seq"],
        }

    def _apply_protest_filed(self, e, p):
        self.protests[p["protest_id"]] = {
            "protest_id": p["protest_id"], "meeting_id": p["meeting_id"],
            "round_id": p["round_id"], "attempt_id": p["attempt_id"],
            "team_id": p["team_id"], "reason": p["reason"],
            "status": "open", "ts": e["ts"], "event_seq": e["seq"],
            "resolution": None,
        }

    def _apply_protest_resolved(self, e, p):
        protest = self.protests[p["protest_id"]]
        protest["status"] = p["verdict"]
        protest["resolution"] = {
            "verdict": p["verdict"], "by": p["by"], "note": p.get("note", ""),
            "ts": e["ts"], "event_seq": e["seq"],
        }

    def _apply_result_corrected(self, e, p):
        attempt = self.attempts[p["attempt_id"]]
        correction = {
            "by": p["by"], "reason": p["reason"], "ts": e["ts"],
            "event_seq": e["seq"],
            "corrected_total": p.get("corrected_total"),
            "corrected_checkpoints": p.get("corrected_checkpoints"),
            "linked_event_seq": p.get("linked_event_seq"),
        }
        attempt["corrections"].append(correction)
        attempt["last_correction_seq"] = e["seq"]

    def _apply_result_officialized(self, e, p):
        round_ = self.rounds[p["round_id"]]
        round_["official_ts"] = e["ts"]
        round_["official_seq"] = e["seq"]
        for aid in p["attempt_ids"]:
            attempt = self.attempts[aid]
            attempt["official_ts"] = e["ts"]
            attempt["official_seq"] = e["seq"]

    def _apply_record_ratified(self, e, p):
        record = {
            "record_id": p["record_id"], "attempt_id": p["attempt_id"],
            "meeting_id": p["meeting_id"], "round_id": p["round_id"],
            "team_id": p["team_id"], "event_key": p["event_key"],
            "official_time": p["official_time_seconds"],
            "standard_rule_id": p["standard_rule_id"],
            "precision_rule_id": p["precision_rule_id"],
            "ratified_by": p["ratified_by"], "ratified_ts": e["ts"],
            "ratified_seq": e["seq"],
        }
        self.records[p["record_id"]] = record
        self._records_by_attempt[p["attempt_id"]] = record
        self.attempts[p["attempt_id"]]["record"] = record

    def _apply_record_rejected(self, e, p):
        self.attempts[p["attempt_id"]]["record"] = {
            "rejected": True, "by": p["by"], "reason": p["reason"],
            "ts": e["ts"], "event_seq": e["seq"],
        }

    # ======================================================================
    # 辅助计算
    # ======================================================================

    def _team_round(self, team_id, round_id):
        team = self.teams.get(team_id)
        round_ = self.rounds.get(round_id)
        _require(team is not None, f"队伍不存在：{team_id}")
        _require(round_ is not None, f"轮次不存在：{round_id}")
        _require(team["meeting_id"] == round_["meeting_id"],
                 "队伍与轮次不属于同一赛事")
        return team, round_

    def _attempt(self, attempt_id):
        attempt = self.attempts.get(attempt_id)
        _require(attempt is not None, f"尝试不存在：{attempt_id}")
        return attempt

    def _confirmation_current(self, attempt):
        """裁判确认必须晚于最近一次成绩更正。"""
        ref = attempt["referee"]
        if not ref:
            return None
        if attempt["last_correction_seq"] and \
                ref["event_seq"] < attempt["last_correction_seq"]:
            return None
        return ref

    def _verification_state(self, attempt):
        """返回 (pending, reasons)：缺感或双通道不一致即待核，不补值。"""
        reasons = []
        race_ts = self._race_ts(attempt)
        precision = self._active_rule(self.precision_rules, race_ts)
        for phase in CHECKPOINTS:
            channels = attempt["readings"].get(phase, {})
            for channel in CHANNELS:
                if (phase, channel) in {(m["phase"], m["channel"])
                                        for m in attempt["missing"]}:
                    reasons.append(f"{phase}/{channel} 传感器缺失，待核")
                elif channel not in channels:
                    reasons.append(f"{phase}/{channel} 读数未到")
            if all(c in channels for c in CHANNELS):
                primary = channels["primary"]["value"]
                backup = channels["backup"]["value"]
                if precision is None:
                    reasons.append("尝试发生时无生效精度规则，无法核对双通道")
                elif abs(primary - backup) > \
                        precision["channel_tolerance_seconds"] + 1e-12:
                    reasons.append(
                        f"{phase} 双通道差异 {abs(primary - backup)}s 超过容差 "
                        f"{precision['channel_tolerance_seconds']}s")
        return (bool(reasons), reasons)

    def _checkpoint_value(self, attempt, phase):
        """检查点取值：以主通道为准，仅用于派生展示；缺感返回 None 不补值。"""
        reading = attempt["readings"].get(phase, {}).get("primary")
        return None if reading is None else reading["value"]

    def _derived_legs(self, attempt):
        split = self._checkpoint_value(attempt, "split")
        finish = self._checkpoint_value(attempt, "finish")
        return {
            "leg1_seconds": split,
            "leg2_seconds": None if (split is None or finish is None)
                             else round(finish - split, 6),
            "total_seconds": finish,
            "note": "由主通道原始检查点派生，缺失即留空，不做插补",
        }

    def _official_time(self, attempt):
        if attempt["corrections"]:
            total = attempt["corrections"][-1]["corrected_total"]
            if total is not None:
                return total
        return self._checkpoint_value(attempt, "finish")

    def _race_ts(self, attempt):
        """比赛时刻取终点主通道读数时间，缺则退回尝试开始时间。"""
        finish = attempt["readings"].get("finish", {}).get("primary")
        if finish:
            return _parse_ts(finish["occurred_ts"])
        return _parse_ts(attempt["started_ts"])

    def _active_rule(self, rules, at_ts):
        """生效规则：effective_from <= 时刻 中最近发布的一条；之后更新不追溯。"""
        chosen = None
        for rule in rules:
            eff = _parse_ts(rule["effective_from"])
            if eff <= at_ts and (chosen is None or eff > _parse_ts(chosen["effective_from"])
                                 or (eff == _parse_ts(chosen["effective_from"])
                                     and rule["event_seq"] > chosen["event_seq"])):
                chosen = rule
        return chosen

    def _covering_cert(self, device_id, channel, lane, race_ts):
        for cert in self.calibrations.values():
            if cert["device_id"] != device_id or cert["channel"] != channel:
                continue
            if cert["lane"] not in (lane, "*"):
                continue
            if _parse_ts(cert["valid_from"]) <= race_ts \
                    <= _parse_ts(cert["valid_until"]):
                return cert
        return None

    def _cert_view(self, device_id, channel, cert_no):
        cert = self.calibrations[(device_id, channel, cert_no)]
        return {k: cert[k] for k in (
            "cert_no", "lane", "device_id", "channel", "verified_by",
            "issued_at", "valid_from", "valid_until", "digest")}

    def _protest_status(self, attempt, at_ts):
        items = [pr for pr in self.protests.values()
                 if pr["attempt_id"] == attempt["attempt_id"]]
        window = self.meetings[attempt["meeting_id"]]["protest_window_seconds"]
        deadline = self._race_ts(attempt) + timedelta(seconds=window)
        resolved = [pr for pr in items if pr["status"] in ("upheld", "rejected")]
        open_ = [pr for pr in items if pr["status"] == "open"]
        at = _parse_ts(at_ts) if at_ts else None
        return {
            "deadline_ts": deadline.isoformat(),
            "window_seconds": window,
            "open": bool(open_),
            "has_resolved": bool(resolved),
            "any_upheld": any(pr["status"] == "upheld" for pr in items),
            "after_deadline": (at > deadline) if at else None,
            "protests": [{
                "protest_id": pr["protest_id"], "team_id": pr["team_id"],
                "status": pr["status"], "reason": pr["reason"],
                "filed_ts": pr["ts"],
                "resolution": None if not pr["resolution"] else {
                    "verdict": pr["resolution"]["verdict"],
                    "by": pr["resolution"]["by"],
                    "ts": pr["resolution"]["ts"],
                },
            } for pr in items],
        }

    def _locate_record(self, record_id, attempt_id, time_seconds):
        if record_id is not None:
            rec = self.records.get(record_id)
            _require(rec is not None, f"纪录不存在：{record_id}")
            return rec
        if attempt_id is not None:
            rec = self._records_by_attempt.get(attempt_id)
            _require(rec is not None, f"该尝试没有已核准纪录：{attempt_id}")
            return rec
        if time_seconds is not None:
            matches = [r for r in self.records.values()
                       if abs(r["official_time"] - float(time_seconds)) < 1e-9]
            _require(matches, f"没有成绩为 {time_seconds}s 的已核准纪录")
            _require(len(matches) == 1,
                     f"成绩 {time_seconds}s 对应多条纪录，请用 record_id 定位")
            return matches[0]
        raise DomainError("必须提供 record_id、attempt_id 或 time_seconds 之一")
