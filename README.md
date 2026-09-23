# 攀岩接力纪录核准

保存速度接力原始计时、设备校准、裁判确认和纪录核准过程。服务接收双通道计时器、裁判终端与设备检定信息，将原始读数按发生顺序封存为哈希链只追加事件；网络重传不会产生第二次有效攀爬，传感器缺失只标记待核、绝不自动补值。

## 领域规则

与 `domain_contract.json` 一一对应：

- **原始读数只追加**：每个检查点（一号起跑反应、交接点、二号反应、终点）× 每通道（主/备）只能封存一条；更正另发 `result_corrected` 事件并链接前一版本，原值保留。
- **重传幂等**：摄入请求携带 `idem_key`，重传返回首次封存的同一条事件（`duplicated: true`），不新增读数。
- **发生顺序**：每台设备的 `channel_seq` 必须严格递增，乱序/重放一律拒收；全部事件用 SHA-256 `prev_hash` 串联，篡改任一字段即断链（`seal.chain_verified=false`，封存文件拒绝重放）。
- **缺感待核**：传感器缺失只登记 `sensor_missing_marked`，尝试进入「待核验」，派生分段留空、不插补；待核尝试不能正式化，更不能核准纪录。
- **轮次身份**：预赛 / 四分之一决赛 / 半决赛 / 决赛各自独立（`heat`/`quarterfinal`/`semifinal`/`final`），尝试归属唯一轮次；只有决赛可核准纪录。
- **前因后果**：换人（`lineup_changed` 链接原棒次）、犯规判罚与撤销、重赛准许与撤销、成绩更正全部是新事件并带 `linked_event_seq`；重赛产生新尝试并以 `supersedes` 链接旧尝试，被接替的尝试不得核准。
- **规则时效**：纪录标准与精度规则按 `effective_from` 版本化，只适用于生效后（以比赛时刻判定）的尝试，赛后收紧规则不追溯。
- **三级发布隔离**：即时成绩（`GET /attempts/{id}/live`）、正式赛果（`GET /rounds/{id}/results`，需裁判确认 + 轮次正式化）、已核准纪录（`GET /records`、`/records/evidence`）使用独立状态与接口。
- **核准清单**：技术代表签发前逐项核对两名选手棒次、每段双通道读数（容差取比赛时生效的精度规则）、起跑反应（抢跑阈值）、覆盖比赛时刻的校准证书、裁判确认（更正后需重新确认）、申诉期已截止且无未决/成立申诉、成绩达到生效标准。

## 证据包

`GET /records/evidence?time=9.58`（也支持 `record_id` / `attempt_id`）返回：

- `climber_order`：实际接力棒次；
- `raw_segments`：8 条原始分段读数（设备、通道序号、发生时间、封存事件号、幂等键）；
- `calibration_certificates`：每台设备在比赛时刻有效的校准证书（编号、检定员、有效期、摘要）；
- `referee_confirmation`：裁判、确认时间、确认时棒次；
- `protest_status`：申诉明细、裁决与申诉截止时间；
- `corrections` / `lineage`：更正链与重赛血缘；
- `rule_versions_at_race`：该尝试适用的标准与精度规则版本；
- `seal`：封存链尾哈希与完整性校验结果。

## 接口

写接口均为 `POST` JSON，封存成功返回事件号与哈希；领域拒绝返回 `422`，字段缺失返回 `400`。

| 接口 | 说明 |
| --- | --- |
| `POST /admin/meetings` `/rounds` `/teams` `/teams/lineup` `/teams/lineup-change` | 赛事、四轮次、报名名单、棒次与换人 |
| `POST /admin/calibrations` | 设备校准证书（有效期、检定员、摘要） |
| `POST /admin/standards` `/admin/precision-rules` | 纪录标准与精度规则版本（生效时间） |
| `POST /attempts` | 开始一次攀爬（可带 `supersedes` 表示重赛接替） |
| `POST /attempts/{id}/readings` | 原始读数（`channel_seq` + `idem_key`） |
| `POST /attempts/{id}/missing` | 传感器缺失登记（待核，不补值） |
| `POST /attempts/{id}/referee-confirmation` `/false-start` `/false-start/revoke` `/rerun` `/rerun/void` `/corrections` | 裁判终端命令 |
| `POST /rounds/{id}/officialize` | 轮次正式化（待核尝试列入 `held_back`） |
| `POST /protests` `/protests/{id}/resolve` | 申诉与裁决 |
| `POST /records/ratify` `/records/reject` | 技术代表签发 / 驳回 |
| `GET /attempts/{id}/live` · `/rounds/{id}/results` · `/records` · `/records/evidence` | 三级读模型 |
| `GET /health` · `/contract` | 运行检查与领域契约 |

## 运行

```bash
python3 service.py --check                      # 配置与契约自检
python3 service.py --port 8000                  # 进程内封存
python3 service.py --port 8000 --log-path data/sealed.jsonl   # 落盘，重启自动重放校验
python3 -m unittest -v                          # 全部测试（19 个）
```

## 代码结构

- `domain_contract.json`：参与者、轮次、状态、事件类型、读模型、核准清单与不变量；
- `eventlog.py`：`SealedLog`——哈希链只追加日志、幂等键去重、设备序号递增、JSONL 持久化与篡改检测；
- `domain.py`：`RecordService`——命令封存、事件重放投影、核准门禁与证据包组装；
- `service.py`：标准库 HTTP 接口（无第三方依赖）；
- `test_domain.py`：贵阳站半决赛 9.60 追平不可核准、决赛 9.58 申诉驳回后按清单签发并从 `time=9.58` 查全证据，以及重传、缺感、换人、犯规撤销、重赛、更正重确认、规则不追溯、校准过期、封存篡改等用例；
- `test_api.py`：HTTP 端到端三级发布流程；`test_service.py`：基础契约。
