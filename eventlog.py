"""按发生顺序封存事件的只追加日志。

每条事件携带：
- seq        日志内全局严格递增序号
- ts         来源设备/终端提供的事件时间（ISO8601），用于按发生时间排序
- device_id  来源设备（双通道计时器各通道、裁判终端、检定终端）
- type/payload
- idem_key   摄入请求幂等键，网络重传返回同一事件而不产生第二条
- prev_hash  上一条事件的哈希，形成防篡改哈希链

同一 device_id 的读数类事件要求 channel_seq 严格递增，乱序/重放拒收。
"""

import hashlib
import json
import threading
from pathlib import Path

GENESIS_HASH = "0" * 64
READING_TYPES = {"timer_reading_ingested", "sensor_missing_marked"}


class SealedLogError(Exception):
    """日志拒收事件（重放、乱序、哈希链断裂等）。"""


def _hash_event(prev_hash, body):
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(json.dumps(body, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


class SealedLog:
    """线程安全的只追加封存日志，可选 JSONL 持久化。"""

    def __init__(self, path=None):
        self._lock = threading.RLock()
        self._events = []
        self._idem = {}          # idem_key -> 已封存事件（重传返回同一结果）
        self._device_seq = {}    # device_id -> 最近 channel_seq
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._replay()

    # -- 封存 ---------------------------------------------------------------

    def append(self, event_type, payload, *, ts, device_id, idem_key=None,
               channel_seq=None):
        """封存一条事件。

        返回 (event, duplicated)：duplicated=True 表示命中幂等键的网络重传，
        返回首次封存的同一条事件，不新增任何记录。
        """
        if not isinstance(payload, dict):
            raise SealedLogError("事件载荷必须是对象")
        if not ts or not device_id:
            raise SealedLogError("事件必须携带发生时间与来源设备")

        with self._lock:
            if idem_key is not None:
                first = self._idem.get(idem_key)
                if first is not None:
                    return dict(first), True

            if event_type in READING_TYPES and channel_seq is not None:
                last = self._device_seq.get(device_id)
                if last is not None and channel_seq <= last:
                    raise SealedLogError(
                        f"设备 {device_id} 序号乱序或重放：{channel_seq} <= {last}")

            seq = len(self._events) + 1
            body = {
                "seq": seq, "ts": ts, "device_id": device_id,
                "type": event_type, "payload": payload,
                "channel_seq": channel_seq,
            }
            if idem_key is not None:
                body["idem_key"] = idem_key
            prev_hash = self._events[-1]["hash"] if self._events else GENESIS_HASH
            body["prev_hash"] = prev_hash
            body["hash"] = _hash_event(prev_hash, body)

            self._events.append(body)
            if idem_key is not None:
                self._idem[idem_key] = body
            if event_type in READING_TYPES and channel_seq is not None:
                self._device_seq[device_id] = channel_seq
            if self._path:
                self._persist(body)
            return dict(body), False

    # -- 读取 ---------------------------------------------------------------

    def events(self):
        """按封存顺序返回全部事件的副本。"""
        with self._lock:
            return [dict(e) for e in self._events]

    def by_idem(self, idem_key):
        with self._lock:
            first = self._idem.get(idem_key)
            return dict(first) if first else None

    def verify_chain(self):
        """重新计算整条哈希链，返回断裂点序号；完整返回 None。"""
        with self._lock:
            prev = GENESIS_HASH
            for e in self._events:
                body = {k: v for k, v in e.items() if k != "hash"}
                if e["prev_hash"] != prev or _hash_event(prev, body) != e["hash"]:
                    return e["seq"]
                prev = e["hash"]
        return None

    # -- 持久化 -------------------------------------------------------------

    def _persist(self, body):
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n")

    def _replay(self):
        """从 JSONL 重放，严格校验哈希链与幂等/序号状态。"""
        prev = GENESIS_HASH
        for line_no, line in enumerate(self._path.read_text(
                encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            e = json.loads(line)
            body = {k: v for k, v in e.items() if k != "hash"}
            if e["prev_hash"] != prev or _hash_event(prev, body) != e["hash"]:
                raise SealedLogError(f"封存文件第 {line_no} 行哈希链断裂")
            if e["seq"] != len(self._events) + 1:
                raise SealedLogError(f"封存文件第 {line_no} 行序号不连续")
            if "idem_key" in e:
                if e["idem_key"] in self._idem:
                    raise SealedLogError("封存文件出现重复幂等键")
                self._idem[e["idem_key"]] = e
            if e["type"] in READING_TYPES and e.get("channel_seq") is not None:
                last = self._device_seq.get(e["device_id"])
                if last is not None and e["channel_seq"] <= last:
                    raise SealedLogError(f"封存文件第 {line_no} 行设备序号回退")
                self._device_seq[e["device_id"]] = e["channel_seq"]
            self._events.append(e)
            prev = e["hash"]
