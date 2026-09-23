"""领域服务测试：贵阳站男子速度接力 9.58 核准全链路与各条不变量。"""

import tempfile
import unittest
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain import CHECKPOINTS, CHANNELS, DomainError, RecordService
from eventlog import SealedLog, SealedLogError

BASE = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)

# 每台计时设备的通道序号在整个赛事期间全局递增，按服务实例隔离
_DEVICE_SEQ = weakref.WeakKeyDictionary()


def iso(moment):
    return moment.isoformat()


def next_device_seq(svc, channel):
    counters = _DEVICE_SEQ.setdefault(svc, {})
    counters[channel] = counters.get(channel, 0) + 1
    return counters[channel]


def build_world(svc, *, protest_window=1800):
    """登记贵阳站：规则版本、校准证书、四个轮次、两支队伍与棒次。"""
    t = BASE - timedelta(days=30)
    svc.register_meeting("M-GY2026", "贵阳站", "2026-09-23",
                         ts=iso(BASE - timedelta(hours=2)),
                         protest_window_seconds=protest_window)
    svc.issue_precision_rule(
        "P1", channel_tolerance_seconds=0.005,
        false_start_threshold_seconds=0.100, timing_resolution="0.01",
        effective_from=iso(t), issued_by="技术委员会", ts=iso(BASE - timedelta(hours=2)))
    svc.issue_record_standard(
        "S1", "men_speed_relay", 9.60, effective_from=iso(t),
        issued_by="技术委员会", ts=iso(BASE - timedelta(hours=2)))
    svc.register_calibration(
        "CAL-P-0917", lane=4, device_id="timer-primary-lane4", channel="primary",
        verified_by="检定员-林", issued_at=iso(t),
        valid_from=iso(BASE - timedelta(days=22)),
        valid_until=iso(BASE + timedelta(days=1)),
        digest="sha256:primary-cert-digest", ts=iso(BASE - timedelta(hours=3)))
    svc.register_calibration(
        "CAL-B-0917", lane=4, device_id="timer-backup-lane4", channel="backup",
        verified_by="检定员-林", issued_at=iso(t),
        valid_from=iso(BASE - timedelta(days=22)),
        valid_until=iso(BASE + timedelta(days=1)),
        digest="sha256:backup-cert-digest", ts=iso(BASE - timedelta(hours=3)))
    for code, rid in (("heat", "R-heat"), ("quarterfinal", "R-quarter"),
                      ("semifinal", "R-semi"), ("final", "R-final")):
        svc.schedule_round("M-GY2026", rid, code, heat_no=1,
                           ts=iso(BASE - timedelta(hours=2)))
    svc.register_team("M-GY2026", "CHN", "中国队",
                      ["zhao", "qian", "sun"],
                      ts=iso(BASE - timedelta(hours=2)))
    svc.register_team("M-GY2026", "RIV", "对手队",
                      ["li", "zhou"], ts=iso(BASE - timedelta(hours=2)))
    svc.submit_lineup("CHN", "R-heat", ["zhao", "sun"], ts=iso(BASE - timedelta(hours=1)))
    svc.submit_lineup("CHN", "R-quarter", ["zhao", "sun"], ts=iso(BASE - timedelta(hours=1)))
    svc.submit_lineup("CHN", "R-semi", ["zhao", "qian"], ts=iso(BASE - timedelta(hours=1)))
    # 决赛先提交 sun，开赛前按规则换人为 qian（保留前因后果）
    svc.submit_lineup("CHN", "R-final", ["zhao", "sun"], ts=iso(BASE - timedelta(hours=1)))
    svc.change_lineup("CHN", "R-final", ["zhao", "qian"],
                      reason="sun 热身受伤，启用替补 qian",
                      ts=iso(BASE - timedelta(minutes=40)))
    svc.submit_lineup("RIV", "R-final", ["li", "zhou"], ts=iso(BASE - timedelta(hours=1)))
    return svc


def send_readings(svc, attempt_id, values, *, start=BASE, idem_prefix="rd"):
    """双通道四检查点读数；每台设备序号跨尝试全局连续，按发生顺序封存。"""
    devices = {"primary": "timer-primary-lane4",
               "backup": "timer-backup-lane4"}
    for i, phase in enumerate(CHECKPOINTS):
        for channel in CHANNELS:
            ts = iso(start + timedelta(seconds=i))
            svc.ingest_reading(
                attempt_id, phase, channel, values[phase][channel],
                ts=ts, device_id=devices[channel],
                channel_seq=next_device_seq(svc, channel),
                idem_key=f"{idem_prefix}:{attempt_id}:{phase}:{channel}")


FINAL_VALUES = {
    "reaction1": {"primary": 0.120, "backup": 0.121},
    "split": {"primary": 4.80, "backup": 4.802},
    "reaction2": {"primary": 0.110, "backup": 0.111},
    "finish": {"primary": 9.58, "backup": 9.582},
}
SEMI_VALUES = {
    "reaction1": {"primary": 0.131, "backup": 0.130},
    "split": {"primary": 4.82, "backup": 4.821},
    "reaction2": {"primary": 0.115, "backup": 0.116},
    "finish": {"primary": 9.60, "backup": 9.599},
}


class GuiyangRecordFlowTest(unittest.TestCase):
    """贵阳站：半决赛先追平 9.60 纪录，决赛 9.58 更快并完成核准。"""

    def setUp(self):
        self.svc = build_world(RecordService())
        # 半决赛 9.60 追平（轮次身份决定它不能被核准为世界纪录）
        self.svc.start_attempt("A-semi", "M-GY2026", "R-semi", "CHN", 4,
                               ts=iso(BASE - timedelta(minutes=90)))
        send_readings(self.svc, "A-semi", SEMI_VALUES,
                      start=BASE - timedelta(minutes=90), idem_prefix="semi")
        self.svc.referee_confirm("A-semi", by="裁判-周",
                                 ts=iso(BASE - timedelta(minutes=89)))
        self.svc.officialize_round("R-semi", by="裁判长",
                                   ts=iso(BASE - timedelta(minutes=80)))
        with self.assertRaises(DomainError):
            self.svc.ratify_record("A-semi", "WR-960-semi",
                                   event_key="men_speed_relay",
                                   by="技术代表-马", ts=iso(BASE - timedelta(minutes=70)))

        # 决赛 9.58
        self.svc.start_attempt("A-final", "M-GY2026", "R-final", "CHN", 4,
                               ts=iso(BASE))
        send_readings(self.svc, "A-final", FINAL_VALUES, idem_prefix="final")

    def test_live_is_separate_from_official_and_records(self):
        live = self.svc.live_result("A-final")
        self.assertEqual(live["published"], "live")
        self.assertEqual(live["provisional_total"], 9.58)
        self.assertEqual(live["provisional_legs"]["leg2_seconds"], 4.78)
        with self.assertRaises(DomainError):
            self.svc.round_results("R-final")          # 未正式化
        self.assertEqual(self.svc.list_records()["records"], [])

    def test_full_ratification_gate_and_evidence_from_9_58(self):
        # 裁判确认前不能核准
        with self.assertRaises(DomainError):
            self.svc.ratify_record("A-final", "WR-958",
                                   event_key="men_speed_relay",
                                   by="技术代表-马", ts=iso(BASE + timedelta(minutes=31)))
        self.svc.referee_confirm("A-final", by="裁判-周",
                                 ts=iso(BASE + timedelta(seconds=20)))
        self.svc.officialize_round("R-final", by="裁判长",
                                   ts=iso(BASE + timedelta(minutes=5)))

        # 申诉期未过不能提前核准
        with self.assertRaises(DomainError):
            self.svc.ratify_record("A-final", "WR-958",
                                   event_key="men_speed_relay",
                                   by="技术代表-马", ts=iso(BASE + timedelta(minutes=10)))
        # 对手申诉被驳回；窗口内仍不可核准
        self.svc.file_protest(
            "P-1", "M-GY2026", "R-final", "A-final", "RIV",
            "怀疑交接计时异常", ts=iso(BASE + timedelta(minutes=8)))
        with self.assertRaises(DomainError):
            self.svc.ratify_record("A-final", "WR-958",
                                   event_key="men_speed_relay",
                                   by="技术代表-马", ts=iso(BASE + timedelta(minutes=12)))
        self.svc.resolve_protest("P-1", verdict="rejected", by="仲裁组",
                                 ts=iso(BASE + timedelta(minutes=20)))
        with self.assertRaises(DomainError):
            self.svc.ratify_record("A-final", "WR-958",
                                   event_key="men_speed_relay",
                                   by="技术代表-马", ts=iso(BASE + timedelta(minutes=25)))

        # 申诉截止后签发
        event, duplicated = self.svc.ratify_record(
            "A-final", "WR-958", event_key="men_speed_relay",
            by="技术代表-马", ts=iso(BASE + timedelta(minutes=31)))
        self.assertFalse(duplicated)
        records = self.svc.list_records()["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["official_time_seconds"], 9.58)

        # 从 9.58 这一条记录必须能查到全部证据
        evidence = self.svc.record_evidence(time_seconds=9.58)
        self.assertEqual(evidence["record"]["record_id"], "WR-958")
        self.assertEqual(evidence["climber_order"], ["zhao", "qian"])
        self.assertEqual(len(evidence["raw_segments"]), 8)
        cert_nos = {c["cert_no"] for c in evidence["calibration_certificates"]}
        self.assertEqual(cert_nos, {"CAL-P-0917", "CAL-B-0917"})
        self.assertEqual(evidence["referee_confirmation"]["by"], "裁判-周")
        self.assertEqual(evidence["referee_confirmation"]["confirmed_climbers"],
                         ["zhao", "qian"])
        protest = evidence["protest_status"]
        self.assertFalse(protest["open"])
        self.assertEqual(protest["protests"][0]["status"], "rejected")
        self.assertIsNotNone(protest["deadline_ts"])
        self.assertEqual(evidence["rule_versions_at_race"]["record_standard"]["rule_id"], "S1")
        self.assertEqual(evidence["rule_versions_at_race"]["precision_rule"]["rule_id"], "P1")
        self.assertTrue(evidence["seal"]["chain_verified"])

        # 同一条记录三种定位方式等价
        self.assertEqual(
            self.svc.record_evidence(record_id="WR-958")["record"]["record_id"],
            "WR-958")
        self.assertEqual(
            self.svc.record_evidence(attempt_id="A-final")["record"]["record_id"],
            "WR-958")


class RetransmissionAndSealTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world(RecordService())
        self.svc.start_attempt("A-x", "M-GY2026", "R-heat", "CHN", 4,
                               ts=iso(BASE - timedelta(hours=3)))

    def test_same_idempotency_key_does_not_add_reading(self):
        kwargs = dict(attempt_id="A-x", phase="finish", channel="primary",
                      value=9.7, ts=iso(BASE - timedelta(hours=3, seconds=4)),
                      device_id="timer-primary-lane4", channel_seq=4)
        first, dup1 = self.svc.ingest_reading(idem_key="net-1", **kwargs)
        self.assertFalse(dup1)
        # 网络重传：完全相同的请求（甚至带"新"序号），返回同一事件
        again, dup2 = self.svc.ingest_reading(idem_key="net-1", **kwargs)
        self.assertTrue(dup2)
        self.assertEqual(again["seq"], first["seq"])
        readings = self.svc.attempts["A-x"]["readings"]["finish"]["primary"]
        self.assertEqual(readings["value"], 9.7)

    def test_different_request_to_same_slot_is_rejected(self):
        self.svc.ingest_reading("A-x", "finish", "primary", 9.7,
                                ts=iso(BASE - timedelta(hours=3, seconds=4)),
                                device_id="timer-primary-lane4",
                                channel_seq=4, idem_key="net-1")
        with self.assertRaises(DomainError):
            self.svc.ingest_reading("A-x", "finish", "primary", 9.8,
                                    ts=iso(BASE - timedelta(hours=3, seconds=4)),
                                    device_id="timer-primary-lane4",
                                    channel_seq=5, idem_key="net-2")

    def test_channel_seq_must_advance(self):
        self.svc.ingest_reading("A-x", "reaction1", "primary", 0.12,
                                ts=iso(BASE - timedelta(hours=3, seconds=1)),
                                device_id="timer-primary-lane4",
                                channel_seq=1, idem_key="k-1")
        with self.assertRaises(DomainError):
            self.svc.ingest_reading("A-x", "split", "primary", 4.8,
                                    ts=iso(BASE - timedelta(hours=3, seconds=2)),
                                    device_id="timer-primary-lane4",
                                    channel_seq=1, idem_key="k-2")

    def test_tampering_breaks_hash_chain(self):
        send_readings(self.svc, "A-x", FINAL_VALUES,
                      start=BASE - timedelta(hours=3), idem_prefix="x")
        self.assertIsNone(self.svc.log.verify_chain())
        sealed = self.svc.log.events()[-1]
        sealed["payload"]["value"] = 9.00
        self.assertEqual(self.svc.log.verify_chain(), sealed["seq"])


class MissingSensorTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world(RecordService())
        self.svc.start_attempt("A-m", "M-GY2026", "R-final", "CHN", 4,
                               ts=iso(BASE))

    def test_missing_sensor_only_pending_never_filled(self):
        values = dict(FINAL_VALUES)
        devices = {"primary": "timer-primary-lane4",
                   "backup": "timer-backup-lane4"}
        # 先封存除 split/backup 外的全部读数；每台设备各自严格递增
        for i, phase in enumerate(CHECKPOINTS):
            for channel in CHANNELS:
                if phase == "split" and channel == "backup":
                    continue
                self.svc.ingest_reading(
                    "A-m", phase, channel, values[phase][channel],
                    ts=iso(BASE + timedelta(seconds=i)),
                    device_id=devices[channel],
                    channel_seq=next_device_seq(self.svc, channel),
                    idem_key=f"m:{phase}:{channel}")
        self.svc.mark_sensor_missing(
            "A-m", "split", "backup", reason="备份通道交接点光电门离线",
            ts=iso(BASE + timedelta(seconds=1)),
            device_id="timer-backup-lane4",
            channel_seq=next_device_seq(self.svc, "backup"),
            idem_key="m:miss")

        live = self.svc.live_result("A-m")
        self.assertEqual(live["state"], "待核验")
        self.assertTrue(any("split/backup" in r for r in live["pending_reasons"]))
        # 缺感不补值：主通道分段仍可用，但状态保持待核
        self.assertEqual(live["provisional_legs"]["leg1_seconds"], 4.80)
        missing = self.svc.attempts["A-m"]["missing"][0]
        self.assertNotIn("value", missing)

        # 裁判即使确认、轮次也不能正式化该尝试，更不能核准纪录
        self.svc.referee_confirm("A-m", by="裁判-周",
                                 ts=iso(BASE + timedelta(seconds=20)))
        with self.assertRaises(DomainError):
            self.svc.officialize_round("R-final", by="裁判长",
                                       ts=iso(BASE + timedelta(minutes=5)))
        with self.assertRaises(DomainError):
            self.svc.ratify_record("A-m", "WR-x", event_key="men_speed_relay",
                                   by="技术代表-马",
                                   ts=iso(BASE + timedelta(minutes=31)))


class FoulRerunCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_world(RecordService())

    def test_false_start_revocation_keeps_both_events(self):
        self.svc.start_attempt("A-fs", "M-GY2026", "R-heat", "CHN", 4,
                               ts=iso(BASE - timedelta(hours=4)))
        send_readings(self.svc, "A-fs", FINAL_VALUES,
                      start=BASE - timedelta(hours=4), idem_prefix="fs")
        self.svc.declare_false_start("A-fs", leg=1, reaction_seconds=0.083,
                                     by="裁判-周", ts=iso(BASE - timedelta(hours=4, seconds=2)))
        self.assertEqual(self.svc.attempts["A-fs"]["status"], "false_start")
        # 复核录像撤销判罚，原判事件保留
        self.svc.revoke_false_start("A-fs", by="裁判长",
                                    reason="起跑传感器误触发，录像显示合法",
                                    ts=iso(BASE - timedelta(hours=4, minutes=5)))
        attempt = self.svc.attempts["A-fs"]
        self.assertEqual(attempt["status"], "recorded")
        self.assertTrue(attempt["false_start"]["revoked"])
        kinds = [e["type"] for e in self.svc.log.events()]
        self.assertIn("false_start_declared", kinds)
        self.assertIn("false_start_revoked", kinds)

    def test_rerun_lineage_and_void(self):
        self.svc.start_attempt("A-q1", "M-GY2026", "R-quarter", "CHN", 4,
                               ts=iso(BASE - timedelta(hours=2)))
        send_readings(self.svc, "A-q1", FINAL_VALUES,
                      start=BASE - timedelta(hours=2), idem_prefix="q1")
        self.svc.grant_rerun("A-q1", by="裁判长",
                             reason="赛道异物干扰", ts=iso(BASE - timedelta(hours=2, minutes=2)))
        # 重赛准许撤销后恢复原尝试
        self.svc.void_rerun("A-q1", by="技术代表-马",
                            reason="复盘认定干扰不成立",
                            ts=iso(BASE - timedelta(hours=2, minutes=3)))
        self.assertEqual(self.svc.attempts["A-q1"]["status"], "recorded")
        # 再次准许并重赛
        self.svc.grant_rerun("A-q1", by="裁判长", reason="复议后认定干扰成立",
                             ts=iso(BASE - timedelta(hours=2, minutes=4)))
        self.svc.start_attempt("A-q2", "M-GY2026", "R-quarter", "CHN", 4,
                               ts=iso(BASE - timedelta(hours=1, minutes=50)),
                               supersedes="A-q1")
        send_readings(self.svc, "A-q2", FINAL_VALUES,
                      start=BASE - timedelta(hours=1, minutes=50),
                      idem_prefix="q2")
        self.svc.referee_confirm("A-q2", by="裁判-周",
                                 ts=iso(BASE - timedelta(hours=1, minutes=49)))
        results = self.svc.round_results  # 未正式化前读取会报错
        with self.assertRaises(DomainError):
            results("R-quarter")
        self.svc.officialize_round("R-quarter", by="裁判长",
                                   ts=iso(BASE - timedelta(hours=1, minutes=45)))
        official = self.svc.round_results("R-quarter")
        self.assertEqual([r["attempt_id"] for r in official["results"]], ["A-q2"])
        # 被接替的旧尝试不能再核准
        with self.assertRaises(DomainError):
            self.svc.ratify_record("A-q1", "WR-q1",
                                   event_key="men_speed_relay",
                                   by="技术代表-马",
                                   ts=iso(BASE - timedelta(hours=1, minutes=10)))

    def test_correction_preserves_raw_and_requires_reconfirmation(self):
        self.svc.start_attempt("A-c", "M-GY2026", "R-final", "CHN", 4,
                               ts=iso(BASE))
        values = {
            "reaction1": {"primary": 0.120, "backup": 0.121},
            "split": {"primary": 4.81, "backup": 4.811},
            "reaction2": {"primary": 0.110, "backup": 0.110},
            "finish": {"primary": 9.59, "backup": 9.591},
        }
        send_readings(self.svc, "A-c", values, idem_prefix="c")
        self.svc.referee_confirm("A-c", by="裁判-周",
                                 ts=iso(BASE + timedelta(seconds=20)))
        self.svc.correct_result("A-c", by="裁判长",
                                reason="终点影像复核，修正终点读数",
                                corrected_total=9.58,
                                ts=iso(BASE + timedelta(minutes=6)))
        # 原始读数未被覆盖
        raw_finish = self.svc.attempts["A-c"]["readings"]["finish"]["primary"]
        self.assertEqual(raw_finish["value"], 9.59)
        # 旧确认在更正之后失效，正式化被挡
        with self.assertRaises(DomainError):
            self.svc.officialize_round("R-final", by="裁判长",
                                       ts=iso(BASE + timedelta(minutes=7)))
        self.svc.referee_confirm("A-c", by="裁判-周",
                                 note="确认更正后成绩",
                                 ts=iso(BASE + timedelta(minutes=8)))
        self.svc.officialize_round("R-final", by="裁判长",
                                   ts=iso(BASE + timedelta(minutes=9)))
        self.assertEqual(self.svc.round_results("R-final")["results"][0]
                         ["official_time_seconds"], 9.58)


class RuleTemporalEffectTest(unittest.TestCase):
    def test_new_rules_do_not_apply_retroactively(self):
        svc = build_world(RecordService())
        svc.start_attempt("A-r", "M-GY2026", "R-final", "CHN", 4, ts=iso(BASE))
        send_readings(svc, "A-r", FINAL_VALUES, idem_prefix="r")
        svc.referee_confirm("A-r", by="裁判-周", ts=iso(BASE + timedelta(seconds=20)))
        svc.officialize_round("R-final", by="裁判长", ts=iso(BASE + timedelta(minutes=5)))
        # 比赛后才发布更严的标准与精度规则
        svc.issue_record_standard(
            "S2", "men_speed_relay", 9.55,
            effective_from=iso(BASE + timedelta(minutes=6)),
            issued_by="技术委员会", ts=iso(BASE + timedelta(minutes=6)))
        svc.issue_precision_rule(
            "P2", channel_tolerance_seconds=0.0005,
            false_start_threshold_seconds=0.150, timing_resolution="0.001",
            effective_from=iso(BASE + timedelta(minutes=6)),
            issued_by="技术委员会", ts=iso(BASE + timedelta(minutes=6)))
        # 9.58 在新规则下不达标，但仍按比赛时的 S1(9.60)/P1 核准
        svc.ratify_record("A-r", "WR-958-old-rules",
                          event_key="men_speed_relay", by="技术代表-马",
                          ts=iso(BASE + timedelta(minutes=31)))
        evidence = svc.record_evidence(record_id="WR-958-old-rules")
        self.assertEqual(evidence["rule_versions_at_race"]["record_standard"]["rule_id"], "S1")
        self.assertEqual(evidence["rule_versions_at_race"]["precision_rule"]["rule_id"], "P1")
        self.assertEqual(evidence["record"]["standard_rule_id"], "S1")


class CalibrationGateTest(unittest.TestCase):
    def test_expired_or_missing_calibration_blocks_ratification(self):
        svc = build_world(RecordService())
        # 校准证书在比赛前到期：直接构造无证书覆盖的场景
        svc2 = RecordService()
        t = BASE - timedelta(days=30)
        svc2.register_meeting("M2", "测试站", "2026-09-23",
                              ts=iso(BASE - timedelta(hours=2)))
        svc2.issue_precision_rule(
            "P1", channel_tolerance_seconds=0.005,
            false_start_threshold_seconds=0.100, timing_resolution="0.01",
            effective_from=iso(t), issued_by="x", ts=iso(BASE - timedelta(hours=2)))
        svc2.issue_record_standard("S1", "men_speed_relay", 9.60,
                                   effective_from=iso(t), issued_by="x",
                                   ts=iso(BASE - timedelta(hours=2)))
        svc2.schedule_round("M2", "F", "final", ts=iso(BASE - timedelta(hours=2)))
        svc2.register_team("M2", "T", "队", ["a", "b"],
                           ts=iso(BASE - timedelta(hours=2)))
        svc2.submit_lineup("T", "F", ["a", "b"], ts=iso(BASE - timedelta(hours=1)))
        svc2.start_attempt("A", "M2", "F", "T", 4, ts=iso(BASE))
        send_readings(svc2, "A", FINAL_VALUES, idem_prefix="nocert")
        svc2.referee_confirm("A", by="j", ts=iso(BASE + timedelta(seconds=20)))
        svc2.officialize_round("F", by="j", ts=iso(BASE + timedelta(minutes=5)))
        with self.assertRaises(DomainError) as error:
            svc2.ratify_record("A", "WR-x", event_key="men_speed_relay",
                               by="td", ts=iso(BASE + timedelta(minutes=31)))
        self.assertIn("校准证书", str(error.exception))


class PersistenceTest(unittest.TestCase):
    def test_jsonl_replay_rebuilds_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sealed.jsonl"
            svc = build_world(RecordService(log_path=path))
            svc.start_attempt("A-final", "M-GY2026", "R-final", "CHN", 4,
                              ts=iso(BASE))
            send_readings(svc, "A-final", FINAL_VALUES, idem_prefix="final")
            svc.referee_confirm("A-final", by="裁判-周",
                                ts=iso(BASE + timedelta(seconds=20)))

            restored = RecordService(log_path=path)
            self.assertEqual(
                restored.live_result("A-final")["provisional_total"], 9.58)
            self.assertIsNone(restored.log.verify_chain())
            # 重放后重传仍然幂等
            _event, duplicated = restored.ingest_reading(
                "A-final", "finish", "primary", 9.58,
                ts=iso(BASE + timedelta(seconds=3)),
                device_id="timer-primary-lane4", channel_seq=4,
                idem_key="final:A-final:finish:primary")
            self.assertTrue(duplicated)

    def test_tampered_file_refuses_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sealed.jsonl"
            svc = RecordService(log_path=path)
            svc.register_meeting("M", "站", "2026-09-23",
                                 ts=iso(BASE - timedelta(hours=2)))
            del svc
            lines = path.read_text(encoding="utf-8").splitlines()
            import json as _json
            row = _json.loads(lines[0])
            row["payload"]["name"] = "被篡改的站名"
            path.write_text(_json.dumps(row, ensure_ascii=False) + "\n",
                            encoding="utf-8")
            with self.assertRaises(SealedLogError):
                RecordService(log_path=path)


if __name__ == "__main__":
    unittest.main()
