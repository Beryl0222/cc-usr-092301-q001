# 商场趣味赛安全联动

连锁商业体中庭趣味赛事（拼豆、甩拖鞋、嗑瓜子等）的运行系统内核：
统一管理**场地版本、玩法风险、报名资格、分时入场、候补、现场分组、裁判记录、
奖品、商户协作、客流观测、应急处置与素材授权**。

系统以事件流为唯一事实来源，基线确认的事件信封
（`event_id/kind/occurred_at/subject_id/version`，见 `fixtures/event.json`）
保持兼容，并在此之上建立了完整事件目录与不变量内核。

## 设计要点

- **容量两本账**：参赛者与围观者容量独立核算、互不挤占。
- **名额不凭空产生**：只有未到场且截止前的取消归还名额；已到场取消、迟到弃权、
  重复取消消息都不释放名额，候补不能顶补。
- **扫码幂等**：`client_record_id` 全局去重，重复扫码与断网补签只生效一次；
  补签按实际扫描时刻判定迟到与暂停，不按同步时刻。
- **应急只影响受影响区域**：消防通道/邻铺拥堵仅暂停涉事区域，其他区域照常；
  现场安排（引导/就地等待/顺延）只发给暂停时确已到场者，未到场者收改期通知。
- **资格与消费脱钩**：资格规则只有年龄、监护、健康申报；体验指标只有到场与停留。
- **未成年人与素材**：监护授权、可撤回；敏感材料仅限事件实际处置人在有效期内访问，
  不可对外发布。
- **开场前就绪闸门**：场地、人员、预案三块逐项确认齐备方可冻结开场。

详见 [`docs/domain.md`](docs/domain.md)。

## 代码结构

```
src/events.py       事件目录（50 类，单一事实来源）+ 信封/payload 校验
src/projection.py   事件 → 状态投影，写入时强制全部不变量（违反即 Rejected）
src/policy.py       只读策略：容量视图、就绪缺口、暂停影响面、顾客视图、体验指标
src/schema.py       由目录生成 JSON Schema
scripts/build_fixture.py  生成完整样例事件流（99 个事件）并重放自检
contracts/          event.schema.json（基线信封）、events.schema.json（完整目录）
fixtures/           event.json（基线样例）、session_flow.json（完整赛事流）
tests/              信封契约 + 26 项不变量/契约/样例重放测试
```

## 本地检查

```bash
python -m unittest discover -s tests   # 全部测试
python -m src.schema                   # 目录变更后重建 JSON Schema
python -m scripts.build_fixture        # 目录/规则变更后重建样例流
```
