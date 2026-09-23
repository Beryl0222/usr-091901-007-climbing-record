"""领域核心业务规则测试。

以“贵阳站决赛”为主线：先有一次 9.58 追平、再有一次更快成绩，
技术代表在签发前必须核对接力顺序、分段计时、起跑反应、校准证书与申诉状态。
"""

import unittest

from domain import DomainError, RecordService

# 标准时间线：校准与规则先于比赛生效，尝试发生在 2000，裁定与核准在其后。
T_CAL = 900
T_RULE = 500
T_MEET = 1000
T_RUN = 2000
T_JUDGE = 2030
T_WINDOW = 2100
T_PROPOSE = 2200
T_RATIFY = 2300


def build_meet(svc, *, finish_primary="9.58", finish_backup="9.5802",
               attempt_id="A1", team_id="T1", race_id="R-F1", round_id="final",
               expected_splits=1, register_attempt=True,
               primary_device="TP", backup_device="TB",
               threshold="9.58", precision=2, effective_at=0, rule_version="v1"):
    """搭好一场有完整证据链的决赛；返回各实体 id 便于断言。"""
    svc.register_round(round_id, at=T_MEET)
    svc.register_race(race_id, round_id, at=T_MEET + 1, lane=4)
    svc.register_team(team_id, at=T_MEET + 2, name="贵阳队")
    svc.register_team("T2", at=T_MEET + 2, name="客队")
    svc.announce_team(race_id, team_id, ["甲", "乙"], at=T_MEET + 3)
    svc.register_calibration("CERT-P", primary_device, 0, at=T_CAL,
                             channel="primary", note="主计时光电门检定")
    svc.register_calibration("CERT-B", backup_device, 0, at=T_CAL,
                             channel="backup", note="备用计时光电门检定")
    svc.register_rule_version(rule_version, at=T_RULE, effective_at=effective_at,
                              record_threshold=threshold, precision=precision,
                              expected_splits=expected_splits)
    if register_attempt:
        register_climb(svc, attempt_id, race_id, team_id,
                       finish_primary=finish_primary, finish_backup=finish_backup,
                       primary_device=primary_device, backup_device=backup_device)
    return attempt_id


def register_climb(svc, attempt_id, race_id, team_id, *,
                   finish_primary="9.58", finish_backup="9.5802",
                   send_backup_finish=True, primary_device="TP",
                   backup_device="TB", reaction="0.145", at=T_RUN,
                   climber_order=None, splits_primary=("5.21",),
                   splits_backup=("5.22",)):
    svc.register_attempt(attempt_id, race_id, team_id, at=at,
                         climber_order=climber_order)
    svc.record_reading(attempt_id, "referee-terminal", f"rx-{attempt_id}",
                       "reaction", reaction, at + 10)
    svc.record_reading(attempt_id, "timer-primary", f"sg-p-{attempt_id}",
                       "start-gate", "0.0", at + 10, device_id=primary_device)
    svc.record_reading(attempt_id, "timer-backup", f"sg-b-{attempt_id}",
                       "start-gate", "0.0", at + 10, device_id=backup_device)
    for i, value in enumerate(splits_primary):
        svc.record_reading(attempt_id, "timer-primary", f"sp-p-{attempt_id}-{i}",
                           "split", value, at + 11 + i,
                           device_id=primary_device, segment=f"leg{i + 1}")
    for i, value in enumerate(splits_backup):
        svc.record_reading(attempt_id, "timer-backup", f"sp-b-{attempt_id}-{i}",
                           "split", value, at + 11 + i,
                           device_id=backup_device, segment=f"leg{i + 1}")
    svc.record_reading(attempt_id, "referee-terminal", f"ho-{attempt_id}",
                       "handoff", at + 12, at + 12)
    # 备用通道先到、主通道后到：验证按发生时间而非接收顺序封存。
    if send_backup_finish:
        svc.record_reading(attempt_id, "timer-backup", f"fin-b-{attempt_id}",
                           "finish", finish_backup, at + 18, device_id=backup_device)
    svc.record_reading(attempt_id, "timer-primary", f"fin-p-{attempt_id}",
                       "finish", finish_primary, at + 19, device_id=primary_device)
    return attempt_id


def confirm_and_close(svc, attempt_id, race_id, *, climber_order=("甲", "乙"),
                      at=T_JUDGE, window_at=T_WINDOW):
    svc.judge_confirm(attempt_id, "start", at=at, judge_id="J1", note="出发合规")
    svc.judge_confirm(attempt_id, "handoff-order", at=at + 1, judge_id="J1",
                      detail={"climber_order": list(climber_order)})
    svc.judge_confirm(attempt_id, "result", at=at + 2, judge_id="J1",
                      note="成绩有效")
    if window_at is not None:
        svc.close_appeal_window(race_id, at=window_at)


def ratify(svc, attempt_id, race_id, *, proposal_id=None,
           technical_delegate="TD-贵阳"):
    proposed = svc.propose_record(attempt_id, at=T_PROPOSE, proposal_id=proposal_id)
    pid = proposed["proposal_id"]
    svc.ratify_record(pid, at=T_RATIFY, technical_delegate=technical_delegate)
    return pid


class FullRatificationTest(unittest.TestCase):
    def setUp(self):
        self.svc = RecordService()

    def test_tie_then_break_and_full_traceability(self):
        svc = self.svc
        build_meet(svc, finish_primary="9.58", finish_backup="9.5802",
                   attempt_id="A-tie")
        confirm_and_close(svc, "A-tie", "R-F1")
        tied = ratify(svc, "A-tie", "R-F1", proposal_id="rec-958")
        self.assertEqual(tied, "rec-958")

        # 随后另一场决赛更快的攀爬：9.56 打破纪录。
        svc.register_race("R-F2", "final", at=T_MEET + 5, lane=5)
        svc.announce_team("R-F2", "T2", ["客1", "客2"], at=T_MEET + 6)
        register_climb(svc, "A-fast", "R-F2", "T2",
                       finish_primary="9.56", finish_backup="9.5598",
                       climber_order=["客1", "客2"])
        confirm_and_close(svc, "A-fast", "R-F2", climber_order=("客1", "客2"),
                          at=2040, window_at=2101)
        ratify(svc, "A-fast", "R-F2", proposal_id="rec-956")

        # 技术代表从 9.58 这一条记录必须能查到全部证据。
        record = svc.record_view("rec-958")
        self.assertEqual(record["status"], "ratified")
        self.assertEqual(record["finish_time"], 9.58)
        self.assertEqual(record["comparison"], "tied")
        self.assertEqual(record["climber_order"], ["甲", "乙"])

        cert_ids = {c["cert_id"] for c in record["calibration_certificates"]}
        self.assertEqual(cert_ids, {"CERT-P", "CERT-B"})
        self.assertTrue(all(not c["revoked"] for c in record["calibration_certificates"]))

        types = {(r["reading_type"], r["channel"]) for r in record["raw_readings"]}
        self.assertIn(("finish", "primary"), types)
        self.assertIn(("finish", "backup"), types)
        self.assertIn(("split", "primary"), types)
        self.assertIn(("reaction", None), types)
        # 裁判确认包含起跑、接力顺序与成绩三类。
        kinds = {c["kind"] for c in record["judge_confirmations"]}
        self.assertEqual(kinds, {"start", "handoff-order", "result"})
        # 申诉截止状态可见，且所有引用的事件序号都在封存链上。
        self.assertEqual(record["appeals"]["window_closed_at"], T_WINDOW)
        self.assertEqual(record["appeals"]["items"], [])
        all_seqs = {e["seq"] for e in svc.events()}
        self.assertTrue(record["sealed_event_refs"])
        self.assertTrue(set(record["sealed_event_refs"]) <= all_seqs)

        fast_record = svc.record_view("rec-956")
        self.assertEqual(fast_record["comparison"], "broken")
        self.assertEqual(fast_record["finish_time"], 9.56)
        self.assertEqual(fast_record["round_id"], "final")

    def test_ratify_blocks_until_every_check_passes(self):
        svc = self.svc
        build_meet(svc, attempt_id="A1")
        # 未裁判确认、未关申诉窗口：一次列出全部阻断项，而不是只报一条。
        svc.propose_record("A1", at=T_PROPOSE, proposal_id="rec-x")
        with self.assertRaises(DomainError) as cm:
            svc.ratify_record("rec-x", at=T_RATIFY, technical_delegate="TD")
        self.assertEqual(cm.exception.code, "conflict")
        reasons = "|".join(cm.exception.reasons)
        self.assertIn("接力顺序", reasons)
        self.assertIn("最终确认", reasons)
        self.assertIn("申诉窗口", reasons)
        # 证据不齐时提案停在“待核验”，没有被错误签发。
        self.assertEqual(svc.list_records(status="pending-verification")[0]["record_id"],
                         "rec-x")


class SealingAndDedupTest(unittest.TestCase):
    def setUp(self):
        self.svc = RecordService()
        build_meet(self.svc, attempt_id="A1")

    def test_sealed_order_follows_occurrence_then_seq(self):
        instant = self.svc.instant_view("A1")
        order = instant["sealed_order"]
        occurred = [r["occurred_at"] for r in order]
        self.assertEqual(occurred, sorted(occurred))
        # 备用到顶（at+18）必须排在主到顶（at+19）之前，尽管调用顺序相反。
        finishes = [r for r in order if r["reading_type"] == "finish"]
        self.assertEqual([f["source"] for f in finishes],
                         ["timer-backup", "timer-primary"])

    def test_retransmission_seals_once_and_adds_no_climb(self):
        svc = self.svc
        before = svc.sealed_count
        # 原样重传：返回同一 seq、标记 duplicate。
        again = svc.record_reading("A1", "timer-primary", "fin-p-A1",
                                   "finish", "9.58", T_RUN + 19, device_id="TP")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["seq"], before)  # seq 即封存序号，未新增
        self.assertEqual(svc.sealed_count, before)
        # 即使重传包残缺（漏带 device_id），也在去重处被吞掉，不会产生第二条读数。
        malformed = svc.record_reading("A1", "timer-primary", "fin-p-A1",
                                       "finish", "9.58", T_RUN + 19)
        self.assertTrue(malformed["duplicate"])
        self.assertEqual(svc.sealed_count, before)
        instant = svc.instant_view("A1")
        self.assertEqual(len([r for r in instant["sealed_order"]
                              if r["reading_type"] == "finish"]), 2)

    def test_dual_channels_never_merged(self):
        svc = self.svc
        # 裁判终端不能直接上报通道型分段读数。
        with self.assertRaises(DomainError):
            svc.record_reading("A1", "referee-terminal", "x", "split", "5.2",
                               T_RUN + 5)
        instant = svc.instant_view("A1")
        self.assertEqual(instant["channels"]["primary"]["finish"], 9.58)
        self.assertAlmostEqual(instant["channels"]["backup"]["finish"], 9.5802)


class MissingSensorTest(unittest.TestCase):
    def setUp(self):
        self.svc = RecordService()
        build_meet(self.svc, attempt_id="A1")

    def test_missing_sensor_pending_never_auto_filled(self):
        svc = self.svc
        # 备用通道到顶传感器无信号：这一攀只有主通道到顶读数。
        svc.announce_team("R-F1", "T2", ["客1", "客2"], at=T_MEET + 4)
        register_climb(svc, "A2", "R-F1", "T2",
                       finish_primary="9.57", send_backup_finish=False,
                       climber_order=["客1", "客2"])
        svc.mark_reading_missing("A2", "finish", at=T_RUN + 40, channel="backup",
                                 sensor_id="TB", note="备用光电门无信号")
        self.assertEqual(svc.instant_view("A2")["state"], "待核验")
        # 系统任何读模型里都没有为备用通道补出数值。
        self.assertIsNone(svc.instant_view("A2")["channels"]["backup"]["finish"])

        # 待核未处理时，裁判不得确认正式成绩。
        with self.assertRaises(DomainError) as cm:
            svc.judge_confirm("A2", "result", at=T_JUDGE, judge_id="J1")
        self.assertEqual(cm.exception.code, "conflict")

        # 人工核验只登记“核验过”，不写任何数值；核验后才允许确认与核准。
        svc.judge_confirm("A2", "start", at=T_JUDGE, judge_id="J1")
        svc.judge_confirm("A2", "handoff-order", at=T_JUDGE + 1, judge_id="J1",
                          detail={"climber_order": ["客1", "客2"]})
        svc.judge_confirm("A2", "adjudicate-missing", at=T_JUDGE + 2, judge_id="J2",
                          detail={"reading_type": "finish", "channel": "backup"},
                          note="核验录像，备用门故障，以主通道为准")
        svc.judge_confirm("A2", "result", at=T_JUDGE + 3, judge_id="J1")
        svc.close_appeal_window("R-F1", at=T_WINDOW)
        pid = ratify(svc, "A2", "R-F1", proposal_id="rec-957")
        record = svc.record_view(pid)
        missing = record["missing_sensors"][0]
        self.assertEqual(missing["reading_type"], "finish")
        self.assertEqual(missing["channel"], "backup")
        self.assertIsNotNone(missing["adjudication"]["judge_id"])
        # 档案里备用通道依然没有数值——只有缺失标记与人工核验痕迹。
        backup_finish = [r for r in record["raw_readings"]
                         if r["reading_type"] == "finish" and r["channel"] == "backup"]
        self.assertEqual(backup_finish, [])

    def test_missing_without_adjudication_blocks(self):
        svc = self.svc
        svc.mark_reading_missing("A1", "split", at=T_RUN + 40, channel="backup",
                                 sensor_id="TB", note="分段点漏采")
        svc.judge_confirm("A1", "start", at=T_JUDGE, judge_id="J1")
        svc.judge_confirm("A1", "handoff-order", at=T_JUDGE + 1, judge_id="J1",
                          detail={"climber_order": ["甲", "乙"]})
        # 待核未处理：成绩确认被拒，尝试保持待核验，也无法进入纪录流程。
        with self.assertRaises(DomainError):
            svc.judge_confirm("A1", "result", at=T_JUDGE + 2, judge_id="J1")
        self.assertEqual(svc.instant_view("A1")["state"], "待核验")
        with self.assertRaises(DomainError):
            svc.propose_record("A1", at=T_PROPOSE)


class RuleVersionTest(unittest.TestCase):
    def test_rules_apply_only_after_effective_time(self):
        svc = RecordService()
        # 先只登记 3000 才生效的新标准：2000 的尝试无规则可用。
        build_meet(svc, attempt_id="A-old", effective_at=3000,
                   threshold="9.58", precision=2)
        with self.assertRaises(DomainError) as cm:
            svc.propose_record("A-old", at=2500)
        self.assertEqual(cm.exception.code, "conflict")
        # 补登 0 点生效的旧标准（9.60），旧尝试只按旧标准评判。
        svc.register_rule_version("v0", at=400, effective_at=0,
                                  record_threshold="9.60", precision=2)
        proposed = svc.propose_record("A-old", at=2500)
        self.assertEqual(proposed["comparison"], "broken")  # 9.58 优于 9.60
        # 新标准 4000 生效、精度 3 位；之后的尝试按新标准取整，旧纪录档案规则版本不变。
        svc.register_rule_version("v2", at=3500, effective_at=4000,
                                  record_threshold="9.58", precision=3)
        svc.register_race("R-F2", "final", at=3800, lane=5)
        svc.announce_team("R-F2", "T2", ["客1", "客2"], at=3801)
        register_climb(svc, "A-new", "R-F2", "T2", at=5000,
                       finish_primary="9.5791", finish_backup="9.5790",
                       climber_order=["客1", "客2"])
        p = svc.propose_record("A-new", at=5200)
        self.assertEqual(p["finish_time"], 9.5791)
        self.assertEqual(svc.record_view(p["proposal_id"])["rule_version"], "v2")
        # 旧尝试的档案规则版本仍是旧标准——规则更新没有追溯改写。
        self.assertEqual(svc._records["rec-A-old"]["rule_version"], "v0")

    def test_half_up_precision_threshold(self):
        svc = RecordService()
        build_meet(svc, attempt_id="A1", finish_primary="9.585")
        # 9.585 按 HALF_UP 取两位为 9.59，未达到 9.58 的标准 → 不能提报。
        with self.assertRaises(DomainError):
            svc.propose_record("A1", at=T_PROPOSE)


class AuditTrailTest(unittest.TestCase):
    def setUp(self):
        self.svc = RecordService()
        build_meet(self.svc, attempt_id="A1")

    def test_substitution_keeps_roster_history(self):
        svc = self.svc
        result = svc.announce_team("R-F1", "T1", ["甲", "丙"], at=T_MEET + 10,
                                   note="乙受伤换人")
        self.assertTrue(result["substitution"])
        roster_events = [e for e in svc.events() if e["event"] == "team-announced"]
        self.assertEqual(len(roster_events), 2)
        self.assertEqual(roster_events[-1]["data"]["previous_climber_order"], ["甲", "乙"])

    def test_rerun_supersedes_without_deleting(self):
        svc = self.svc
        reading_seqs_before = [e["seq"] for e in svc.events()
                               if e["event"] == "reading-recorded"
                               and e["data"]["attempt_id"] == "A1"]
        svc.order_rerun("R-F1", ["A1"], at=2050, reason="赛道设备干扰")
        self.assertTrue(svc.instant_view("A1")["superseded"])
        # 原始读数一条不少。
        reading_seqs_after = [e["seq"] for e in svc.events()
                              if e["event"] == "reading-recorded"
                              and e["data"]["attempt_id"] == "A1"]
        self.assertEqual(reading_seqs_before, reading_seqs_after)
        register_climb(svc, "A2", "R-F1", "T1",
                       finish_primary="9.57", finish_backup="9.5704")
        confirm_and_close(svc, "A2", "R-F1", at=2060, window_at=2110)
        svc.propose_record("A2", at=2200, proposal_id="rec-rerun")
        svc.ratify_record("rec-rerun", at=2300, technical_delegate="TD")
        official = svc.official_view("R-F1")
        self.assertTrue(official["results"][0]["superseded"])
        self.assertEqual(official["rankings"][0]["attempt_id"], "A2")

    def test_disqualification_and_rescind_chain(self):
        svc = self.svc
        svc.disqualify("A1", at=2040, reason="抢跑", official_id="J9")
        self.assertTrue(svc.official_view("R-F1")["results"][0]["disqualified"])
        with self.assertRaises(DomainError):
            svc.propose_record("A1", at=T_PROPOSE)
        svc.rescind_disqualification("A1", at=2045, reason="录像显示未抢跑",
                                     official_id="TD")
        self.assertFalse(svc.official_view("R-F1")["results"][0]["disqualified"])
        confirm_and_close(svc, "A1", "R-F1")
        pid = ratify(svc, "A1", "R-F1", proposal_id="rec-dq")
        decisions = svc.record_view(pid)["disqualifications"]
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0]["active"])
        self.assertIsNotNone(decisions[0]["rescind_seq"])

    def test_result_correction_keeps_cause_and_previous_value(self):
        svc = self.svc
        svc.correct_result("A1", "9.57", at=2040,
                           reason="主通道终点误触，核对备用与录像后更正",
                           official_id="J1")
        confirm_and_close(svc, "A1", "R-F1")
        pid = ratify(svc, "A1", "R-F1", proposal_id="rec-corr")
        view = svc.record_view(pid)
        self.assertEqual(view["finish_time"], 9.57)
        self.assertEqual(view["corrections"][0]["previous_finish"], 9.58)
        self.assertEqual(view["corrections"][0]["corrected_finish"], 9.57)
        # 即时成绩仍是原始读数，不反映更正——两类读模型分离。
        self.assertEqual(svc.instant_view("A1")["channels"]["primary"]["finish"], 9.58)
        self.assertTrue(svc.official_view("R-F1")["results"][0]["corrected"])


class CalibrationAndAppealTest(unittest.TestCase):
    def test_missing_or_revoked_calibration_blocks(self):
        svc = RecordService()
        # 一场完全没登记校准的比赛。
        svc.register_round("final", at=T_MEET)
        svc.register_race("R-FX", "final", at=T_MEET + 1)
        svc.register_team("TX", at=T_MEET + 2, name="无校准队")
        svc.announce_team("R-FX", "TX", ["甲", "乙"], at=T_MEET + 3)
        svc.register_rule_version("v1", at=T_RULE, effective_at=0,
                                  record_threshold="9.58", precision=2)
        register_climb(svc, "AX", "R-FX", "TX",
                       climber_order=["甲", "乙"])
        confirm_and_close(svc, "AX", "R-FX")
        svc.propose_record("AX", at=T_PROPOSE, proposal_id="rec-nocal")
        with self.assertRaises(DomainError) as cm:
            svc.ratify_record("rec-nocal", at=T_RATIFY, technical_delegate="TD")
        self.assertTrue(any("校准证书" in r for r in cm.exception.reasons))

        # 证书登记后立即被撤销：撤销状态如实留痕，核准仍然阻断。
        svc.register_calibration("CP", "TP", 0, at=T_CAL)
        svc.register_calibration("CB", "TB", 0, at=T_CAL)
        svc.revoke_calibration("CP", at=T_CAL + 1, reason="复检不合格")
        svc.revoke_calibration("CB", at=T_CAL + 1, reason="复检不合格")
        with self.assertRaises(DomainError) as cm:
            svc.ratify_record("rec-nocal", at=T_RATIFY + 3, technical_delegate="TD")
        self.assertTrue(any("校准证书" in r for r in cm.exception.reasons))

    def test_appeal_lifecycle_blocks_and_restores(self):
        svc = RecordService()
        build_meet(svc, attempt_id="A1")
        confirm_and_close(svc, "A1", "R-F1", window_at=None)
        self.assertEqual(svc.official_view("R-F1")["state"], "正式赛果")
        svc.file_appeal("R-F1", at=2080, filed_by="T2", grounds="质疑接棒顺序",
                        appeal_id="AP1")
        self.assertEqual(svc.instant_view("A1")["state"], "申诉中")
        svc.propose_record("A1", at=T_PROPOSE, proposal_id="rec-ap")
        with self.assertRaises(DomainError) as cm:
            svc.ratify_record("rec-ap", at=T_RATIFY, technical_delegate="TD")
        self.assertTrue(any("未决申诉" in r for r in cm.exception.reasons))
        # 窗口也还没关：两项阻断并存。
        self.assertTrue(any("申诉窗口" in r for r in cm.exception.reasons))
        svc.resolve_appeal("AP1", at=2090, outcome="rejected", note="录像维持原判")
        self.assertEqual(svc.instant_view("A1")["state"], "正式赛果")
        # 裁决后再关窗，即可核准；关窗后不再受理申诉。
        svc.close_appeal_window("R-F1", at=2100)
        svc.ratify_record("rec-ap", at=T_RATIFY, technical_delegate="TD")
        with self.assertRaises(DomainError):
            svc.file_appeal("R-F1", at=2101, filed_by="T2", grounds="逾期申诉")


class RoundIdentityAndSealedRecordTest(unittest.TestCase):
    def test_rounds_are_distinct_identities(self):
        svc = RecordService()
        with self.assertRaises(DomainError):
            svc.register_race("R1", "semifinal", at=1)  # 轮次未登记
        svc.register_round("heat", at=1)
        svc.register_round("semifinal", at=1)
        svc.register_race("RH", "heat", at=2)
        svc.register_race("RS", "semifinal", at=2)  # 各自轮次身份下的独立场次
        self.assertEqual(svc.official_view("RH")["round_id"], "heat")
        self.assertEqual(svc.official_view("RS")["round_id"], "semifinal")
        with self.assertRaises(DomainError):
            svc.register_round("lap", at=3)  # 非法轮次身份
        with self.assertRaises(DomainError):
            svc.register_round("heat", at=4)  # 同一轮次不得重复登记

    def test_ratified_attempt_is_sealed(self):
        svc = RecordService()
        build_meet(svc, attempt_id="A1")
        confirm_and_close(svc, "A1", "R-F1")
        pid = ratify(svc, "A1", "R-F1", proposal_id="rec-sealed")
        self.assertEqual(svc.instant_view("A1")["state"], "纪录已核准")
        with self.assertRaises(DomainError):
            svc.record_reading("A1", "timer-primary", "late", "split", "1.0",
                               T_RUN + 99, device_id="TP")
        with self.assertRaises(DomainError):
            svc.correct_result("A1", "9.50", at=2400, reason="试图改已核准成绩",
                               official_id="X")
        with self.assertRaises(DomainError):
            svc.ratify_record(pid, at=2400, technical_delegate="TD2")  # 禁止重复签发


if __name__ == "__main__":
    unittest.main()
