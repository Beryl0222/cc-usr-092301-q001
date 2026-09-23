"""生成 fixtures/session_flow.json：一场中庭趣味赛的完整事件流。

该流覆盖从场地版本到赛后奖品的完整生命周期，且*必须*能被内核原样重放
（tests/test_schema.py 会重放两遍验证幂等）。重建：

    python -m scripts.build_fixture
"""
from __future__ import annotations

import json
from pathlib import Path

from src.projection import Kernel

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "session_flow.json"


class Emitter:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self._seq = 0

    def emit(self, kind, subject, payload, at):
        self._seq += 1
        ver = sum(1 for e in self.events if e["subject_id"] == subject) + 1
        self.events.append({
            "event_id": f"0923-{self._seq:04d}",
            "kind": kind,
            "occurred_at": at,
            "subject_id": subject,
            "version": ver,
            "payload": payload,
        })


def build() -> list[dict]:
    em = Emitter()
    e = em.emit
    SID = "s-0923-pm"
    VV, GAME = "vv-L1-0923", "g-beans"

    def t(h, m):
        return f"2026-09-23T{h:02d}:{m:02d}:00+08:00"

    # —— 场地版本与玩法 ——
    e("VENUE_VERSION_PUBLISHED", VV, {
        "venue_version_id": VV, "store_id": "store-7", "floor": "L1",
        "zones": [
            {"zone_id": "z-atrium", "name": "主中庭", "role": "playing",
             "competitor_capacity": 8, "spectator_capacity": 20,
             "adjacent_zone_ids": ["z-neighbor"]},
            {"zone_id": "z-neighbor", "name": "北侧空地", "role": "playing",
             "competitor_capacity": 8, "spectator_capacity": 20,
             "adjacent_zone_ids": ["z-atrium"]},
            {"zone_id": "z-egress", "name": "消防通道", "role": "egress",
             "competitor_capacity": 0, "spectator_capacity": 0,
             "adjacent_zone_ids": ["z-atrium"]},
        ],
    }, t(9, 0))
    e("GAME_REGISTERED", GAME, {
        "game_id": GAME, "name": "拼豆速拼", "risk_level": "LOW",
        "team_size_min": 2, "team_size_max": 2,
        "age_min": 6, "age_max": 99, "guardian_required": True,
    }, t(9, 5))
    e("GAME_SCHEDULED", SID, {
        "session_id": SID, "game_id": GAME, "venue_version_id": VV,
        "zone_ids": ["z-atrium", "z-neighbor", "z-egress"],
        "slot_start": t(14, 0), "slot_end": t(15, 0),
        "competitor_capacity": 6, "spectator_capacity": 20,
        "roster_deadline": t(12, 0), "late_grace_minutes": 15,
    }, t(9, 10))
    e("ELIGIBILITY_RULE_SET", SID, {"session_id": SID, "rules": [
        {"type": "AGE_MIN", "value": 6},
        {"type": "GUARDIAN_FOR_MINORS"},
        {"type": "HEALTH_DECLARATION"},
    ]}, t(10, 5))

    # —— 报名：3 队录取（满员），2 队候补 ——
    teams = {
        "t-A": [("a-1", False, None), ("a-2", False, None)],
        "t-B": [("b-kid", True, "g-chen"), ("b-dad", False, None)],
        "t-C": [("c-1", False, None), ("c-2", False, None)],
        "t-D": [("d-1", False, None), ("d-2", False, None)],
        "t-E": [("e-1", False, None), ("e-2", False, None)],
    }
    for i, (tid, mem) in enumerate(teams.items(), start=10):
        e("TEAM_REGISTERED", SID, {
            "team_id": tid, "session_id": SID,
            "members": [{"member_id": m, "is_minor": minor,
                         "guardian_id": g} for m, minor, g in mem],
        }, t(10, i))
    for tid in ("t-A", "t-B", "t-C"):
        e("REGISTRATION_ACCEPTED", SID,
          {"team_id": tid, "session_id": SID}, t(10, 40))
    e("WAITLIST_ENTERED", SID,
      {"team_id": "t-D", "session_id": SID, "position": 1}, t(10, 41))
    e("WAITLIST_ENTERED", SID,
      {"team_id": "t-E", "session_id": SID, "position": 2}, t(10, 42))

    # A 队在名单截止前、未到场取消 → 名额释放，队首 D 顶补
    e("TEAM_CANCELLED", SID, {
        "team_id": "t-A", "session_id": SID,
        "cancelled_at": t(11, 0), "had_checked_in": False,
    }, t(11, 0))
    e("WAITLIST_PROMOTED", SID,
      {"team_id": "t-D", "session_id": SID, "reason": "TEAM_CANCELLED_BEFORE_DEADLINE"},
      t(11, 1))

    # 分时入场窗口（入场窗口 10 分钟）
    from datetime import datetime, timedelta

    def plus(hhmm, minutes):
        d = datetime(2026, 9, 23, hhmm[0], hhmm[1], tzinfo=None) + timedelta(minutes=minutes)
        return f"2026-09-23T{d.hour:02d}:{d.minute:02d}:00+08:00"

    for tid, frm in (("t-B", (13, 50)), ("t-C", (13, 50)), ("t-D", (14, 0))):
        e("ENTRY_SLOT_ASSIGNED", SID, {
            "team_id": tid, "session_id": SID,
            "admit_from": t(*frm), "admit_until": plus(frm, 10),
        }, t(11, 20))

    # 奖品与商户任务在准备阶段登记
    e("PRIZE_DEFINED", SID, {"prize_id": "p-part", "session_id": SID,
                             "basis": "PARTICIPATION", "description": "参与徽章"}, t(13, 0))
    e("PRIZE_DEFINED", SID, {"prize_id": "p-rank", "session_id": SID,
                             "basis": "RANK", "description": "冠军券包"}, t(13, 1))
    e("MERCHANT_TASK_ASSIGNED", SID, {
        "task_id": "mk-1", "merchant_id": "m-noodle", "zone_id": "z-neighbor",
        "kind": "QUEUE_SNACK", "window_start": t(14, 0), "window_end": t(15, 0),
    }, t(13, 5))

    # —— 就绪检查三块齐备后冻结 ——
    e("READINESS_CHECK_STARTED", SID,
      {"session_id": SID, "started_at": t(13, 10)}, t(13, 10))
    items = {
        "VENUE": ("floor_marking", "egress_clear", "zone_capacity_plate"),
        "STAFFING": ("referee_assigned", "queue_steward_assigned",
                     "emergency_contact_known"),
        "EMERGENCY_PLAN": ("injury_flow_drilled", "minor_lost_flow_drilled",
                           "media_rule_briefed"),
    }
    minute = 11
    for area, names in items.items():
        for name in names:
            minute += 1
            e("READINESS_ITEM_CONFIRMED", SID, {
                "session_id": SID, "area": area, "item": name,
                "confirmed_by": "manager-7", "confirmed_at": t(13, minute),
            }, t(13, minute))
    e("PLAN_FROZEN", SID, {"session_id": SID, "frozen_at": t(13, 30)}, t(13, 30))

    # —— 素材授权（未成年人由监护人授权） ——
    e("MEDIA_CONSENT_GRANTED", SID, {
        "consent_id": "c-bkid", "member_id": "b-kid",
        "scope": ["ONSITE_DISPLAY", "PUBLIC_PROMOTION"],
        "granted_by": "g-chen", "granted_at": t(13, 35),
    }, t(13, 35))
    e("MEDIA_CONSENT_GRANTED", SID, {
        "consent_id": "c-c1", "member_id": "c-1",
        "scope": ["ONSITE_DISPLAY", "PUBLIC_PROMOTION"],
        "granted_by": "c-1", "granted_at": t(13, 36),
    }, t(13, 36))

    # 商户接单
    e("MERCHANT_TASK_CONFIRMED", SID, {
        "task_id": "mk-1", "merchant_id": "m-noodle", "handled_by": "店长A",
    }, t(13, 40))

    # —— 现场：围观客流与排队公示 ——
    for i in range(6):
        e("ZONE_ENTRY_OBSERVED", SID, {
            "zone_id": "z-atrium", "member_id": f"spec-{i}",
            "audience": "SPECTATOR", "observed_at": t(13, 50),
        }, t(13, 50))
    e("QUEUE_NOTICE_PUBLISHED", SID, {
        "zone_id": "z-atrium", "expected_wait_minutes": 8,
        "observed_at": t(13, 50),
    }, t(13, 50))

    # B 队在线签到；随后同一扫码记录重试（幂等），终端登记去重
    e("CHECKIN_SCANNED", SID, {
        "scan_id": "scan-1001", "session_id": SID, "zone_id": "z-atrium",
        "team_id": "t-B", "member_id": "b-dad", "scanned_at": t(13, 55),
        "scanner_id": "gun-9", "offline": False,
        "client_record_id": "gun-9#1001",
    }, t(13, 55))
    e("CHECKIN_SCANNED", SID, {
        "scan_id": "scan-1001-retry", "session_id": SID, "zone_id": "z-atrium",
        "team_id": "t-B", "member_id": "b-dad", "scanned_at": t(13, 55),
        "scanner_id": "gun-9", "offline": False,
        "client_record_id": "gun-9#1001",
    }, t(13, 55))
    e("CHECKIN_DEDUP_REJECTED", SID, {
        "client_record_id": "gun-9#1001", "session_id": SID,
        "reason": "DUPLICATE_SCAN",
    }, t(13, 56))

    # 断网恢复后补签批次：b-dad 在线已扫（去重），b-kid 首次生效
    e("OFFLINE_BATCH_SYNCED", SID, {
        "batch_id": "batch-gun9-1", "scanner_id": "gun-9",
        "records": [
            {"client_record_id": "gun-9#1001", "zone_id": "z-atrium",
             "team_id": "t-B", "member_id": "b-dad", "scanned_at": t(13, 54)},
            {"client_record_id": "gun-9#1002", "zone_id": "z-atrium",
             "team_id": "t-B", "member_id": "b-kid", "scanned_at": t(13, 57)},
        ],
    }, t(14, 30))

    # 围观者在拥堵初期离场
    for i in range(6):
        e("ZONE_EXIT_OBSERVED", SID, {
            "zone_id": "z-atrium", "member_id": f"spec-{i}",
            "audience": "SPECTATOR", "observed_at": t(14, 2),
        }, t(14, 2))
    e("CROWD_OBSERVED", SID, {
        "zone_id": "z-atrium", "observed_at": t(13, 58),
        "competitors_present": 2, "spectators_present": 18,
        "queue_length": 24, "source": "STEWARD_COUNT",
    }, t(13, 58))
    e("CROWD_THRESHOLD_CROSSED", SID, {
        "zone_id": "z-atrium", "metric": "EGRESS_QUEUE_LENGTH",
        "observed_value": 24, "threshold": 15, "direction": "OVER",
        "observed_at": t(14, 1),
    }, t(14, 1))
    e("QUEUE_NOTICE_PUBLISHED", SID, {
        "zone_id": "z-atrium", "expected_wait_minutes": 20,
        "observed_at": t(14, 2),
    }, t(14, 2))

    # —— 消防通道相邻拥堵：只暂停主中庭 ——
    e("AREA_SUSPENSION_STARTED", SID, {
        "suspension_id": "sp-1", "zone_ids": ["z-atrium"],
        "reason": "ADJACENT_EGRESS_CONGESTION", "issued_by": "duty-mgr-7",
        "started_at": t(14, 5),
    }, t(14, 5))
    e("SESSION_CHANGE_PUBLISHED", SID, {
        "session_id": SID, "change_type": "ZONE_SUSPENDED",
        "message": "主中庭临时暂停，已到场者引导至北侧空地",
        "visible_at": t(14, 6),
    }, t(14, 6))
    # 安排只发给暂停时已到场的 B 队
    e("SUSPENSION_ARRANGEMENT_ISSUED", SID, {
        "suspension_id": "sp-1", "team_ids": ["t-B"],
        "action": "RELOCATE",
        "instruction": {"target_zone_id": "z-neighbor",
                        "note": "工作人员步行引导，赛程顺延 10 分钟"},
        "issued_at": t(14, 6),
    }, t(14, 6))
    # 未到场的 C 队收到改期，而不是现场安排
    e("ENTRY_SLOT_RESCHEDULED", SID, {
        "team_id": "t-C", "session_id": SID,
        "admit_from": t(14, 12), "admit_until": t(14, 22),
        "reason": "ZONE_SUSPENDED",
    }, t(14, 7))
    e("AREA_SUSPENSION_LIFTED", SID, {
        "suspension_id": "sp-1", "ended_at": t(14, 10),
    }, t(14, 10))
    e("SESSION_CHANGE_PUBLISHED", SID, {
        "session_id": SID, "change_type": "ZONE_RESUMED",
        "message": "主中庭已恢复，北侧空地比赛继续",
        "visible_at": t(14, 11),
    }, t(14, 11))

    # C 队在新窗口、宽限期内签到（在北侧空地）
    for mid, mm in (("c-1", "2001"), ("c-2", "2002")):
        e("CHECKIN_SCANNED", SID, {
            "scan_id": f"scan-{mm}", "session_id": SID, "zone_id": "z-neighbor",
            "team_id": "t-C", "member_id": mid, "scanned_at": t(14, 12),
            "scanner_id": "gun-3", "offline": False,
            "client_record_id": f"gun-3#{mm}",
        }, t(14, 12))
    for mid in ("d-1", "d-2"):
        e("CHECKIN_SCANNED", SID, {
            "scan_id": f"scan-d-{mid}", "session_id": SID, "zone_id": "z-neighbor",
            "team_id": "t-D", "member_id": mid, "scanned_at": t(14, 13),
            "scanner_id": "gun-3", "offline": False,
            "client_record_id": f"gun-3#d-{mid}",
        }, t(14, 13))

    # 区域内停留观测（用于停留效果统计）
    for mid in ("b-dad", "b-kid"):
        e("ZONE_ENTRY_OBSERVED", SID, {
            "zone_id": "z-neighbor", "member_id": mid,
            "audience": "COMPETITOR", "observed_at": t(14, 8),
        }, t(14, 8))
    for mid in ("c-1", "c-2", "d-1", "d-2"):
        e("ZONE_ENTRY_OBSERVED", SID, {
            "zone_id": "z-neighbor", "member_id": mid,
            "audience": "COMPETITOR", "observed_at": t(14, 13),
        }, t(14, 13))

    # —— 安全事件 1：受伤（敏感材料仅对处置人开放） ——
    e("INCIDENT_REPORTED", SID, {
        "incident_id": "i-1", "zone_id": "z-neighbor", "kind": "INJURY",
        "severity": "MEDIUM", "reported_by": "staff-1",
        "reported_at": t(14, 15),
    }, t(14, 15))
    e("RESPONDER_ASSIGNED", SID, {
        "incident_id": "i-1", "responder_id": "r-1", "role": "FIRST_AID",
        "assigned_at": t(14, 15),
    }, t(14, 15))
    e("MEDIA_ASSET_CAPTURED", SID, {
        "asset_id": "a-injury-1", "zone_id": "z-neighbor",
        "captured_at": t(14, 16), "consent_ids": ["c-bkid"],
        "incident_id": "i-1", "sensitive": True,
    }, t(14, 16))
    e("ASSET_ACCESS_GRANTED", SID, {
        "asset_id": "a-injury-1", "incident_id": "i-1",
        "responder_id": "r-1", "purpose": "TREATMENT",
        "granted_at": t(14, 17), "expires_at": t(15, 17),
    }, t(14, 17))

    # —— 安全事件 2：未成年人走失，登记监护人领回 ——
    e("INCIDENT_REPORTED", SID, {
        "incident_id": "i-2", "zone_id": "z-neighbor", "kind": "MINOR_LOST",
        "severity": "MEDIUM", "reported_by": "staff-2",
        "reported_at": t(14, 20),
    }, t(14, 20))
    e("RESPONDER_ASSIGNED", SID, {
        "incident_id": "i-2", "responder_id": "r-2", "role": "SECURITY",
        "assigned_at": t(14, 20),
    }, t(14, 20))
    e("MINOR_REUNITED", SID, {
        "incident_id": "i-2", "member_id": "b-kid",
        "guardian_id": "g-chen", "verified_by": "staff-3", "at": t(14, 35),
    }, t(14, 35))
    e("INCIDENT_STATUS_CHANGED", SID, {
        "incident_id": "i-2", "status": "RESOLVED", "at": t(14, 36),
    }, t(14, 36))

    # B 队已到场后退赛：取消不制造名额，候补 E 无法顶补（流中不出现晋升）
    for mid in ("b-dad", "b-kid"):
        e("ZONE_EXIT_OBSERVED", SID, {
            "zone_id": "z-neighbor", "member_id": mid,
            "audience": "COMPETITOR", "observed_at": t(14, 40),
        }, t(14, 40))
    e("TEAM_CANCELLED", SID, {
        "team_id": "t-B", "session_id": SID,
        "cancelled_at": t(14, 45), "had_checked_in": True,
    }, t(14, 45))

    # —— 分组、裁判、改裁（C 与 D） ——
    e("BRACKET_BUILT", SID, {"session_id": SID, "groups": [
        {"group_id": "g-A", "zone_id": "z-neighbor", "heat_no": 1,
         "team_ids": ["t-C", "t-D"]},
    ]}, t(14, 46))
    e("HEAT_CALLED", SID, {
        "session_id": SID, "group_id": "g-A", "called_at": t(14, 47),
    }, t(14, 47))
    e("MATCH_RESULT_RECORDED", SID, {
        "match_id": "m-1", "session_id": SID, "group_id": "g-A",
        "referee_id": "ref-1",
        "rankings": [{"team_id": "t-C", "rank": 1, "score": 42},
                     {"team_id": "t-D", "rank": 2, "score": 39}],
        "recorded_at": t(14, 50),
    }, t(14, 50))
    e("RESULT_AMENDED", SID, {
        "match_id": "m-1", "amendment_id": "am-1", "referee_id": "ref-2",
        "rankings": [{"team_id": "t-D", "rank": 1, "score": 44},
                     {"team_id": "t-C", "rank": 2, "score": 42}],
        "reason": "回看录像确认压线有效", "amended_at": t(14, 55),
    }, t(14, 55))

    # 敏感材料访问随处置结束撤销，事件结案
    e("ASSET_ACCESS_REVOKED", SID, {
        "asset_id": "a-injury-1", "responder_id": "r-1",
        "revoked_at": t(14, 56),
    }, t(14, 56))
    e("INCIDENT_RESOLVED", SID, {
        "incident_id": "i-1",
        "resolution": "冰敷处理后无碍，已告知当班经理并登记",
        "resolved_at": t(14, 57),
    }, t(14, 57))

    # —— 奖品 ——
    for tid in ("t-C", "t-D"):
        e("PRIZE_AWARDED", SID, {
            "prize_id": "p-part", "team_id": tid,
            "awarded_by": "staff-4", "awarded_at": t(15, 0),
        }, t(15, 0))
    e("PRIZE_AWARDED", SID, {
        "prize_id": "p-rank", "team_id": "t-D",
        "awarded_by": "staff-4", "awarded_at": t(15, 2),
    }, t(15, 2))

    # —— 非敏感素材对外发布：授权范围匹配后方可批准 ——
    e("MEDIA_ASSET_CAPTURED", SID, {
        "asset_id": "a-fun-1", "zone_id": "z-neighbor",
        "captured_at": t(14, 58), "consent_ids": ["c-c1"],
        "incident_id": None, "sensitive": False,
    }, t(14, 58))
    for mid in ("c-1", "c-2", "d-1", "d-2"):
        e("ZONE_EXIT_OBSERVED", SID, {
            "zone_id": "z-neighbor", "member_id": mid,
            "audience": "COMPETITOR", "observed_at": t(15, 3),
        }, t(15, 3))
    e("MEDIA_RELEASE_REQUESTED", SID, {
        "request_id": "q-1", "asset_id": "a-fun-1", "channel": "WECHAT",
        "requested_by": "mkt-1", "requested_at": t(15, 40),
    }, t(15, 40))
    e("MEDIA_RELEASE_DECIDED", SID, {
        "request_id": "q-1", "decision": "APPROVE",
        "decided_by": "mkt-lead", "decided_at": t(15, 45),
        "reason": "授权范围含公域宣传，非敏感素材",
    }, t(15, 45))

    # 写盘前用内核完整重放自检
    kernel = Kernel()
    kernel.apply_many(em.events)
    assert kernel.sessions[SID].frozen
    return em.events


def main() -> None:
    events = build()
    OUT.write_text(json.dumps(events, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"wrote {OUT} ({len(events)} events)")


if __name__ == "__main__":
    main()
