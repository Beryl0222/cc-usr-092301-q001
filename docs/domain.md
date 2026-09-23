# 领域模型与不变量

本系统以**事件流**为唯一事实来源。门店、场次、排队、应急、素材授权的所有状态都是
事件的投影；现场可以从事件流完整还原，区域指标与顾客视图也都由投影计算。

## 1. 标识与时间语义（沿用基线信封）

| 字段 | 语义 |
| --- | --- |
| `event_id` | 全局唯一且**幂等**：同一 id 重放只生效一次（网络重试、消息重复投递安全） |
| `kind` | `src/events.py` 目录登记的 50 类事件之一 |
| `occurred_at` | 业务发生时刻（ISO-8601 带时区）。**断网补签也填实际发生时刻**，不是同步时刻 |
| `subject_id` | 聚合主体，绝大多数为场次 `session_id`；场地版本、玩法各自为主体 |
| `version` | 同一 subject 内单调递增，乱序/重放/并发写错版本直接拒绝 |
| `payload` | 事件事实；每类事件的必填字段见事件目录 |

扫码另有第二层幂等键 `client_record_id`：同一台扫码枪的同一次扫描，无论在线直发、
超时重试，还是先离线后进补签批次，全局只产生一次到场效果。

## 2. 聚合与生命周期

```
门店 ──发布──> 场地版本 VENUE_VERSION_PUBLISHED（不可变，改场地=发新版本）
玩法 ──登记──> GAME_REGISTERED（风险级别 LOW/MEDIUM/HIGH + 年龄/监护约束）
场次 ──排期──> GAME_SCHEDULED（绑定场地版本与区域、两类容量、名单截止、迟到宽限）
  ├─ 报名：TEAM_REGISTERED → REGISTRATION_ACCEPTED / WAITLIST_ENTERED → WAITLIST_PROMOTED
  ├─ 入场：ENTRY_SLOT_ASSIGNED / RESCHEDULED → CHECKIN_SCANNED（在线/离线批次）
  ├─ 异常：LATE_ARRIVAL_MARKED / TEAM_CANCELLED / SLOT_FORFEITED
  ├─ 运行：BRACKET_BUILT → HEAT_CALLED → MATCH_RESULT_RECORDED（RESULT_AMENDED 留痕）
  ├─ 奖品：PRIZE_DEFINED(PARTICIPATION/RANK) → PRIZE_AWARDED
  ├─ 协作：MERCHANT_TASK_ASSIGNED/CONFIRMED；QUEUE_NOTICE/SESSION_CHANGE 对顾客可见
  ├─ 客流：CROWD_OBSERVED/THRESHOLD_CROSSED；ZONE_ENTRY/EXIT_OBSERVED（算停留时长）
  ├─ 应急：AREA_SUSPENSION_STARTED → SUSPENSION_ARRANGEMENT_ISSUED → …_LIFTED
  │        INCIDENT_REPORTED → RESPONDER_ASSIGNED → … → INCIDENT_RESOLVED
  ├─ 素材：MEDIA_CONSENT_GRANTED/WITHDRAWN、MEDIA_ASSET_CAPTURED、
  │        ASSET_ACCESS_GRANTED/REVOKED、MEDIA_RELEASE_REQUESTED/DECIDED
  └─ 就绪：READINESS_CHECK_STARTED → READINESS_ITEM_CONFIRMED* → PLAN_FROZEN
```

## 3. 硬性不变量（内核拒绝写入，而非事后告警）

### 容量：参赛者与围观者两本独立账
- 区域容量分 `competitor_capacity` 与 `spectator_capacity`，互不挤占；
  围观区站满不影响参赛者入场，反之亦然。
- 进场/离场成对（`ZONE_ENTRY_OBSERVED`/`ZONE_EXIT_OBSERVED`），重复进场、
  无进场的离场都被拒绝；在场人数由进出事件精确还原。

### 名额：迟到取消与"取消消息"绝不制造名额
- 只有**未到场且在名单截止前**的取消归还名额（`releasable=True`）。
- 已签到后退赛、过宽限未到的弃权（`SLOT_FORFEITED`）：名额作废，
  `seats_used` 不下降，候补无法顶补（`NO_RELEASABLE_SEAT`）。
- 重复的取消/弃权消息被拒绝（`TEAM_NOT_ACTIVE`），不会二次触发任何账变。
- 候补严格按队首晋升（`WAITLIST_ORDER`），位置必须连续（`BAD_WAITLIST_POSITION`）。

### 签到：重复扫码与断网补签
- `client_record_id` 全局去重；在线已扫后离线批次再上送，该条静默忽略。
- 补签按 `scanned_at`（实际时刻）判定：实际扫得晚就是晚，
  实际扫于暂停期间就不生效——同步时刻晚不能把不合规签到"补成"到场。
- 迟到/暂停期间的扫码不写入去重账本，网络恢复或暂停解除后顾客仍可重试。

### 应急：只暂停受影响区域，只安排已到场者
- `AREA_SUSPENSION_STARTED` 的 `zone_ids` 之外的区域照常运行（容量视图仍开放）。
- 暂停开始时内核快照**已到场队伍**；安排（`SUSPENSION_ARRANGEMENT_ISSUED`）
  只能发给这些队伍（`ARRANGEMENT_FOR_NON_PRESENT`），未到场者走改期/公告。
- 引导（RELOCATE）的目标区域必须存在且自身未被暂停。
- 暂停期间该区域扫码不产生到场；解除后扫码恢复有效。

### 资格：系统层面不存在消费门槛
- 资格规则只接受年龄、未成年人监护、健康申报四类
  （`ELIGIBILITY_RULE_UNKNOWN:SPEND_*` 永远被拒）。
- 体验指标（`experience_metrics`）只有到场、迟到/不到、停留时长，没有消费字段。

### 未成年人与素材
- 未成年人报名必须登记监护人；其素材授权必须由登记监护人作出。
- 走失领回（`MINOR_REUNITED`）核验登记监护人，不符即拒。
- 授权可撤回：撤回时刻之后的拍摄被拒（`CAPTURE_AFTER_WITHDRAWAL`），
  对外发布在撤回后被拒（`CONSENT_WITHDRAWN`）。
- 敏感素材必须挂在安全事件下；访问只能授予该事件**实际被指派的处置人**，
  且带用途与到期时间；处置结束可立即撤销。敏感素材一律不得对外发布。
- 对外发布还要匹配授权范围（现场展示 vs 公域宣传）。

### 就绪冻结
- 冻结前必须齐备：场地（地贴/通道清空/容量牌）、人员（裁判/排队疏导/应急联络）、
  预案（受伤与走失流程演练、素材规则宣讲），且场次绑定了比赛/疏散区域。
- 冻结后结构性变更（改场地、改队伍、改排期结构）被拒；
  现场运行事件（签到、客流、暂停、事件、裁判、素材）不受限。整改走 `PLAN_UNFROZEN`。

### 裁判与奖品
- 分组只能纳入已录取/已签到队伍且不重复；改裁（`RESULT_AMENDED`）覆盖当前名次，
  但每次改判连同裁判、原因、时间全部留痕，改判 id 不可重复。
- 参与奖要求实际签到；名次奖要求出现在有效排名中。

## 4. 对三类角色的交付面

| 角色 | 取自 | 能看到/能做 |
| --- | --- | --- |
| 门店 | `readiness_gaps`、`PLAN_FROZEN` | 开场前逐项确认场地/人员/预案，未齐不能冻结开场 |
| 顾客 | `customer_queue_view`、`customer_changes_view` | 真实排队时长（过期公示拒绝写入）；队伍级改期只见于当事队伍 |
| 区域团队 | `experience_metrics`、`zone_capacity_view`、`affected_scope` | 跨楼层/区域比较到场与停留，评估暂停影响面，无消费维度 |

## 5. 目录与契约产物

- `src/events.py`：事件目录（单一事实来源）与信封/payload 校验。
- `src/projection.py`：内核，事件 → 状态投影 + 全部不变量守门。
- `src/policy.py`：只读策略视图（容量、就绪、暂停影响面、顾客视图、体验指标）。
- `src/schema.py`：由目录生成 `contracts/events.schema.json`；
  文件过期会被测试捕获，重建命令 `python -m src.schema`。
- `fixtures/event.json`：基线信封样例（保持兼容）。
- `fixtures/session_flow.json`：一场完整赛事的 99 个事件，由
  `scripts/build_fixture.py` 生成并重放自检；测试会再重放两遍验证幂等。
