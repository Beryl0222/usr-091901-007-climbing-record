# 攀岩接力纪录核准

保存速度接力原始计时、设备校准、裁判确认和纪录核准过程。技术代表在签发世界纪录前，必须核对接力顺序、每段计时、起跑反应、赛道设备校准与申诉状态；本服务把双通道计时器、裁判终端与设备检定信息按发生顺序封存，支撑“即时成绩 / 正式赛果 / 已核准纪录”三类相互独立的对外接口。

## 不可破坏的业务原则

- **原始读数只追加、不覆盖**：每条读数封存为一条事件，按 `(发生时间, 接收序号)` 排序；同 `(来源, 读数标识)` 的网络重传只返回原封存序号，不产生第二次有效攀爬。
- **双通道分别留痕**：`timer-primary` 与 `timer-backup` 独立记录，任何路径都不合并、不用另一通道插值。
- **缺传感器只标待核**：传感器缺失登记为 `reading-marked-missing`，尝试进入“待核验”；只有裁判人工核验（不写任何补值）后才可继续，系统永不自动补值。
- **四轮次独立身份**：预赛 `heat`、四分之一决赛 `quarterfinal`、半决赛 `semifinal`、决赛 `final` 各自登记，跨轮次不混用成绩。
- **前因后果完整保留**：换人（历次出场名单）、重赛（原尝试标记取代但读数保留）、犯规判罚与撤销、成绩更正（原值/新值/原因/官员）全部追加留痕。
- **规则不溯及既往**：纪录标准与取整精度按“尝试发生时已生效”的规则版本选取，规则更新只适用于生效后的尝试。
- **核准门禁**：校准证书缺失/撤销/过期、缺少裁判起跑/接力顺序/成绩确认、申诉窗口未截止或申诉未决、存在生效犯规或待核缺失时，纪录只能停留在 `pending-verification`。
- **一条纪录可完整溯源**：从任一已核准纪录都能查到校准证书、原始分段读数、裁判确认与申诉截止状态，全部引用封存事件序号。

## 运行

```bash
python3 service.py --check          # 核对服务与契约配置
python3 service.py --port 8000      # 启动服务
python3 -m unittest -v              # 运行全部测试（28 个）
```

## 接口

写操作统一为 `POST /commands`（载荷含 `command` 字段），同时提供语义化别名。错误码：`400` 参数非法、`404` 资源不存在、`409` 业务冲突（响应体 `reasons` 列出全部待核事项）。

| 类别 | 命令 / 路由 |
| --- | --- |
| 赛会登记 | `POST /rounds/{round}`、`POST /rounds/{round}/races`、`POST /teams`、`POST /races/{race}/roster` |
| 尝试与读数 | `POST /races/{race}/attempts`、`POST /attempts/{id}/readings`、`POST /attempts/{id}/missing` |
| 校准与规则 | `POST /calibrations`、`POST /calibrations/{cert}/revoke`、`POST /rules` |
| 裁判与申诉 | `POST /attempts/{id}/confirmations`、`POST /races/{race}/appeals`、`POST /appeals/{id}/resolve`、`POST /races/{race}/appeal-window/close` |
| 判罚与更正 | `POST /attempts/{id}/disqualify`、`POST /attempts/{id}/rescind-dq`、`POST /races/{race}/reruns`、`POST /attempts/{id}/corrections` |
| 纪录 | `POST /attempts/{id}/record-proposal`、`POST /records/{id}/ratify`、`POST /records/{id}/reject` |

三类读模型彼此独立：

- `GET /attempts/{id}/instant` —— **即时成绩**：现场接收的双通道读数与封存顺序，反映缺失与待核，不作为正式依据；成绩更正不会改写即时读数。
- `GET /races/{id}/result` —— **正式赛果**：排名、犯规、重赛取代、成绩更正轨迹与申诉状态。
- `GET /records/{id}` —— **已核准纪录档案**：成绩与平/破纪录判定、适用规则版本、校准证书快照、原始读数、缺失传感器及人工核验、全部裁判确认、犯规/更正链、申诉截止状态与封存事件引用。

辅助查询：`GET /records?status=ratified`、`GET /events?from_seq=N`（封存事件流）、`GET /contract`、`GET /health`。

## 典型时间线（贵阳站决赛）

1. 开赛前登记轮次/场次/队伍出场名单（两名选手接力顺序）、上传双通道设备校准证书、登记当时生效的纪录标准（如 9.58、保留 2 位、HALF_UP）。
2. 比赛中封存起跑反应、出发门、分段、接棒与两条通道的到顶读数；网络重传自动幂等。
3. 9.58 追平纪录：裁判确认起跑合规、接力顺序与成绩，申诉期截止且无未决申诉后，技术代表核准；随后一场 9.56 更快成绩按同一流程核准为“打破”。
4. 从 `GET /records/rec-958` 一条记录即可回溯校准证书、原始分段、裁判确认与申诉截止状态。

## 文件

- `domain_contract.json` —— 参与者、轮次、读数来源/类型、读模型、命令、事件与不变量契约。
- `domain.py` —— 事件封存与全部业务规则（线程安全，无第三方依赖）。
- `service.py` —— HTTP 入口、命令路由与三类读模型。
- `test_domain.py` / `test_api.py` / `test_service.py` —— 领域规则、端到端接口与基础契约测试。
