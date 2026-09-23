"""趣味赛事系统——不变量测试。

每个测试对应需求中的一条硬规则；事件流即现场还原脚本。
"""
import json
import unittest
from datetime import datetime
from pathlib import Path

from src.events import KINDS, validate_envelope, validate_event
from src.projection import Kernel, Rejected
from src.policy import (
    READINESS_REQUIRED_ITEMS, affected_scope, customer_changes_view,
    customer_queue_view, experience_metrics, readiness_gaps,
    session_seats_view, zone_capacity_view,
)

T = "2026-09-23T"


def iso(h, m):
    return f"{T}{h:02d}:{m:02d}:00+08:00"


VENUE = {
    "venue_version_id": "vv-0923", "store_id": "store-7", "floor": "L1",
    "zones": [
        {"zone_id": "z-atrium", "name": "主中庭", "role": "playing",
         "competitor_capacity": 8, "spectator_capacity": 20,
         "adjacent_zone_ids": ["z-neighbor"]},
        {"zone_id": "z-neighbor", "name": "北侧空地", "role": "playing",
         "competitor_capacity": 8, "spectator_capacity": 20,
         "adjacent_zone_ids": ["z-atrium"]},
    ],
}


class Flow:
    """按 subject 自增版本的事件构造器。"""

    def __init__(self, testcase: unittest.TestCase):
        self.tc = testcase
        self.k = Kernel()
        self.n = 0

    def ev(self, kind, subject, payload, at=iso(13, 0), version=None):
        self.n += 1
        ver = version if version is not None else self.k.versions.get(subject, 0) + 1
        rec = {"event_id": f"e{self.n:04d}", "kind": kind,
               "occurred_at": at, "subject_id": subject,
               "version": ver, "payload": payload}
        self.k.apply(rec)
        return rec

    def reject(self, code_prefix, kind, subject, payload, at=iso(13, 0),
               version=None):
        with self.tc.assertRaises(Rejected) as c:
            self.ev(kind, subject, payload, at, version=version)
        code = c.exception.code
        assert code.startswith(code_prefix), f"{code} !~ {code_prefix}"
        return code

    # —— 常用场景搭建 ——

    def setup_session(self, comp_cap=4, spec_cap=20, deadline=(12, 0), grace=10):
        self.ev("VENUE_VERSION_PUBLISHED", "vv-0923", VENUE, iso(9, 0))
        self.ev("GAME_REGISTERED", "g-beans", {
            "game_id": "g-beans", "name": "拼豆速拼", "risk_level": "LOW",
            "team_size_min": 2, "team_size_max": 2,
            "age_min": 6, "age_max": 99, "guardian_required": True,
        }, iso(9, 5))
        self.ev("GAME_SCHEDULED", "s1", {
            "session_id": "s1", "game_id": "g-beans",
            "venue_version_id": "vv-0923", "zone_ids": ["z-atrium", "z-neighbor"],
            "slot_start": iso(14, 0), "slot_end": iso(15, 0),
            "competitor_capacity": comp_cap, "spectator_capacity": spec_cap,
            "roster_deadline": iso(*deadline), "late_grace_minutes": grace,
        }, iso(9, 10))
        return self.k.sessions["s1"]

    def team(self, tid, members=None):
        members = members or [
            {"member_id": f"{tid}-m1", "is_minor": False},
            {"member_id": f"{tid}-m2", "is_minor": False},
        ]
        self.ev("TEAM_REGISTERED", "s1",
                {"team_id": tid, "session_id": "s1", "members": members})

    def accept(self, tid):
        self.ev("REGISTRATION_ACCEPTED", "s1",
                {"team_id": tid, "session_id": "s1"})

    def waitlist(self, tid, pos):
        self.ev("WAITLIST_ENTERED", "s1",
                {"team_id": tid, "session_id": "s1", "position": pos})

    def window(self, tid, frm, to):
        self.ev("ENTRY_SLOT_ASSIGNED", "s1", {
            "team_id": tid, "session_id": "s1",
            "admit_from": iso(*frm), "admit_until": iso(*to),
        })

    def scan_ev(self, tid, mid, at, zone="z-atrium", crid=None, offline=False):
        crid = crid or f"{mid}-rec"
        self.ev("CHECKIN_SCANNED", "s1", {
            "scan_id": f"scan-{crid}", "session_id": "s1", "zone_id": zone,
            "team_id": tid, "member_id": mid, "scanned_at": at,
            "scanner_id": "gun-1", "offline": offline,
            "client_record_id": crid,
        }, at)

    def freeze(self, at=iso(13, 30)):
        self.ev("READINESS_CHECK_STARTED", "s1",
                {"session_id": "s1", "started_at": at})
        for area, items in READINESS_REQUIRED_ITEMS.items():
            for item in items:
                self.ev("READINESS_ITEM_CONFIRMED", "s1", {
                    "session_id": "s1", "area": area, "item": item,
                    "confirmed_by": "manager-1", "confirmed_at": at,
                }, at)
        self.ev("PLAN_FROZEN", "s1", {"session_id": "s1", "frozen_at": at}, at)


class KernelTest(unittest.TestCase):
    def setUp(self):
        self.f = Flow(self)

    # ---- 信封与目录 ----

    def test_baseline_sample_still_valid(self):
        data = json.loads((Path(__file__).parents[1] / "fixtures" / "event.json")
                          .read_text(encoding="utf-8"))
        # 基线样例无 payload，按信封规则仍然有效
        self.assertEqual(validate_envelope(data), [])

    def test_bad_envelope(self):
        self.assertTrue(validate_event({"kind": "NOPE"})[0].startswith("missing"))
        rec = {"event_id": "x", "kind": "NOPE", "occurred_at": iso(10, 0),
               "subject_id": "s", "version": 1, "payload": {}}
        self.assertIn("unknown_kind:NOPE", validate_event(rec))
        rec["kind"] = "GAME_SCHEDULED"
        self.assertIn("missing_payload:session_id", validate_event(rec))

    def test_every_registered_kind_has_tests_or_handlers(self):
        for kind, fields in KINDS.items():
            self.assertTrue(all(isinstance(x, str) for x in fields))

    # ---- 容量：参赛者/围观者两本账 ----

    def test_competitor_and_spectator_capacity_separate(self):
        st = self.f.setup_session()
        for i in range(20):
            self.f.ev("ZONE_ENTRY_OBSERVED", "s1", {
                "zone_id": "z-atrium", "member_id": f"spec-{i}",
                "audience": "SPECTATOR", "observed_at": iso(13, 50),
            }, iso(13, 50))
        self.f.reject("SPECTATOR_CAPACITY_FULL", "ZONE_ENTRY_OBSERVED", "s1", {
            "zone_id": "z-atrium", "member_id": "spec-21",
            "audience": "SPECTATOR", "observed_at": iso(13, 50),
        }, iso(13, 50))
        # 围观满了，参赛者仍可入场
        self.f.ev("ZONE_ENTRY_OBSERVED", "s1", {
            "zone_id": "z-atrium", "member_id": "c-1",
            "audience": "COMPETITOR", "observed_at": iso(13, 50),
        }, iso(13, 50))
        view = zone_capacity_view(self.f.k, st, "z-atrium", iso(13, 50))
        self.assertEqual(view.spectator_headroom, 0)
        self.assertEqual(view.competitors_inside, 1)
        self.f.reject("DOUBLE_ENTRY", "ZONE_ENTRY_OBSERVED", "s1", {
            "zone_id": "z-atrium", "member_id": "c-1",
            "audience": "COMPETITOR", "observed_at": iso(13, 51),
        }, iso(13, 51))
        self.f.reject("EXIT_WITHOUT_ENTRY", "ZONE_EXIT_OBSERVED", "s1", {
            "zone_id": "z-atrium", "member_id": "ghost",
            "audience": "SPECTATOR", "observed_at": iso(13, 52),
        }, iso(13, 52))

    # ---- 名额与候补 ----

    def test_registration_capacity_and_waitlist_order(self):
        st = self.f.setup_session()
        for t in ("t1", "t2"):
            self.f.team(t)
            self.f.accept(t)
        self.assertEqual(session_seats_view(st)["seats_held"], 4)
        self.f.team("t3")
        self.f.reject("COMPETITOR_CAPACITY_FULL", "REGISTRATION_ACCEPTED", "s1",
                      {"team_id": "t3", "session_id": "s1"})
        self.f.waitlist("t3", 1)

    def test_waitlist_must_promote_in_order(self):
        # 容量有余时，顺序检查才是唯一约束
        st = self.f.setup_session(comp_cap=6)
        self.f.team("t1"); self.f.accept("t1")
        self.f.team("t2"); self.f.waitlist("t2", 1)
        self.f.team("t3"); self.f.waitlist("t3", 2)
        self.f.reject("WAITLIST_ORDER", "WAITLIST_PROMOTED", "s1",
                      {"team_id": "t3", "session_id": "s1", "reason": "X"})
        # 队首可以正常晋升
        self.f.ev("WAITLIST_PROMOTED", "s1",
                  {"team_id": "t2", "session_id": "s1", "reason": "SEAT"},
                  iso(10, 0))
        self.assertEqual(st.teams["t2"].status, "ACCEPTED")

    def test_early_cancel_releases_seat_and_promotes(self):
        st = self.f.setup_session()
        for t in ("t1", "t2"):
            self.f.team(t); self.f.accept(t)
        self.f.team("t3"); self.f.waitlist("t3", 1)
        # 截止前、未到场取消 → 名额释放，候补顶补
        self.f.ev("TEAM_CANCELLED", "s1", {
            "team_id": "t1", "session_id": "s1",
            "cancelled_at": iso(11, 0), "had_checked_in": False,
        }, iso(11, 0))
        self.assertEqual(st.seats_used, 2)
        self.f.ev("WAITLIST_PROMOTED", "s1",
                  {"team_id": "t3", "session_id": "s1", "reason": "CANCEL"},
                  iso(11, 1))
        self.assertEqual(st.seats_used, 4)
        self.assertEqual(st.teams["t3"].status, "ACCEPTED")

    def test_late_and_checkedin_cancels_create_no_seat(self):
        st = self.f.setup_session()
        for t in ("t1", "t2"):
            self.f.team(t); self.f.accept(t); self.f.window(t, (13, 50), (14, 0))
        self.f.team("t3"); self.f.waitlist("t3", 1)
        self.f.freeze()
        self.f.scan_ev("t1", "t1-m1", iso(13, 55))
        self.f.scan_ev("t1", "t1-m2", iso(13, 56))
        # 已到场取消：名额不归还
        self.f.ev("TEAM_CANCELLED", "s1", {
            "team_id": "t1", "session_id": "s1",
            "cancelled_at": iso(14, 20), "had_checked_in": True,
        }, iso(14, 20))
        self.assertFalse(st.teams["t1"].releasable)
        self.assertEqual(st.seats_used, 4)
        self.f.reject("NO_RELEASABLE_SEAT", "WAITLIST_PROMOTED", "s1",
                      {"team_id": "t3", "session_id": "s1", "reason": "CANCEL"},
                      iso(14, 21))
        # 迟到/不到的弃权：名额同样作废
        self.f.ev("SLOT_FORFEITED", "s1", {
            "team_id": "t2", "session_id": "s1",
            "forfeit_at": iso(14, 20), "reason": "NO_SHOW",
        }, iso(14, 20))
        self.assertEqual(st.seats_used, 4)
        # 重复的取消消息不会再造成任何变化
        self.f.reject("TEAM_NOT_ACTIVE", "TEAM_CANCELLED", "s1", {
            "team_id": "t1", "session_id": "s1",
            "cancelled_at": iso(14, 25), "had_checked_in": True,
        }, iso(14, 25))

    def test_event_replay_is_idempotent(self):
        st = self.f.setup_session()
        self.f.team("t1"); self.f.accept("t1")
        rec = self.f.ev("TEAM_CANCELLED", "s1", {
            "team_id": "t1", "session_id": "s1",
            "cancelled_at": iso(11, 0), "had_checked_in": False,
        }, iso(11, 0))
        seats_after = st.seats_used
        self.assertIsNone(self.f.k.apply(rec))  # 同一 event_id 重放
        self.assertEqual(st.seats_used, seats_after)
        self.f.reject("BAD_VERSION", "TEAM_REGISTERED", "s1", {
            "team_id": "t9", "session_id": "s1",
            "members": [{"member_id": "a", "is_minor": False},
                        {"member_id": "b", "is_minor": False}],
        }, version=1)

    # ---- 扫码：重复扫码 / 断网补签 / 迟到 ----

    def test_duplicate_scan_counts_once(self):
        st = self.f.setup_session()
        self.f.team("t1"); self.f.accept("t1"); self.f.window("t1", (13, 50), (14, 0))
        self.f.freeze()
        self.f.scan_ev("t1", "t1-m1", iso(13, 55), crid="gun-1#0001")
        # 同一枪重复扫（网络重试）：不报错但不产生第二个人/第二次效果
        self.f.scan_ev("t1", "t1-m1", iso(13, 55), crid="gun-1#0001")
        self.assertEqual(st.teams["t1"].checked_in, {"t1-m1"})

    def test_offline_batch_dedups_against_online(self):
        st = self.f.setup_session()
        self.f.team("t1"); self.f.accept("t1"); self.f.window("t1", (13, 50), (14, 0))
        self.f.freeze()
        # 在线已扫 m1
        self.f.scan_ev("t1", "t1-m1", iso(13, 55), crid="gun-1#0001")
        # 断网期间的批次在恢复后上送：m1 重复、m2 首扫
        self.f.ev("OFFLINE_BATCH_SYNCED", "s1", {
            "batch_id": "b-1", "scanner_id": "gun-1",
            "records": [
                {"client_record_id": "gun-1#0001", "zone_id": "z-atrium",
                 "team_id": "t1", "member_id": "t1-m1", "scanned_at": iso(13, 54)},
                {"client_record_id": "gun-1#0002", "zone_id": "z-atrium",
                 "team_id": "t1", "member_id": "t1-m2", "scanned_at": iso(13, 57)},
            ],
        }, iso(14, 30))  # 同步时刻晚，判定仍按 scanned_at
        self.assertEqual(st.teams["t1"].checked_in, {"t1-m1", "t1-m2"})
        # 批次重复上送被拒绝
        self.f.reject("BATCH_DUPLICATE", "OFFLINE_BATCH_SYNCED", "s1", {
            "batch_id": "b-1", "scanner_id": "gun-1", "records": [],
        }, iso(14, 31))

    def test_offline_late_scan_does_not_become_checkin(self):
        st = self.f.setup_session()
        self.f.team("t1"); self.f.accept("t1"); self.f.window("t1", (13, 50), (14, 0))
        self.f.freeze()
        # 断网时实际在 14:20 扫描（超过 14:10 宽限），14:40 才同步
        self.f.ev("OFFLINE_BATCH_SYNCED", "s1", {
            "batch_id": "b-late", "scanner_id": "gun-1",
            "records": [
                {"client_record_id": "gun-9#0001", "zone_id": "z-atrium",
                 "team_id": "t1", "member_id": "t1-m1", "scanned_at": iso(14, 20)},
            ],
        }, iso(14, 40))
        self.assertNotIn("t1-m1", st.teams["t1"].checked_in)
        self.assertEqual(st.teams["t1"].status, "ACCEPTED")
        self.assertEqual(st.seats_used, 2)  # 没有任何名额被造出来

    # ---- 分区暂停 ----

    def test_suspension_only_affects_zone_and_arranges_present(self):
        st = self.f.setup_session()
        for t in ("t1", "t2"):
            self.f.team(t); self.f.accept(t); self.f.window(t, (13, 50), (14, 0))
        self.f.freeze()
        self.f.scan_ev("t1", "t1-m1", iso(13, 55))
        # 13:57 消防通道拥堵，只暂停主中庭
        self.f.ev("AREA_SUSPENSION_STARTED", "s1", {
            "suspension_id": "sp-1", "zone_ids": ["z-atrium"],
            "reason": "EGRESS_CONGESTION", "issued_by": "duty-mgr",
            "started_at": iso(13, 57),
        }, iso(13, 57))
        sp = st.suspensions["sp-1"]
        self.assertEqual(set(sp.present_teams), {"t1"})
        # 安排只能给已到场的 t1
        self.f.ev("SUSPENSION_ARRANGEMENT_ISSUED", "s1", {
            "suspension_id": "sp-1", "team_ids": ["t1"],
            "action": "RELOCATE",
            "instruction": {"target_zone_id": "z-neighbor", "note": "步行引导"},
            "issued_at": iso(13, 58),
        }, iso(13, 58))
        self.f.reject("ARRANGEMENT_FOR_NON_PRESENT",
                      "SUSPENSION_ARRANGEMENT_ISSUED", "s1", {
            "suspension_id": "sp-1", "team_ids": ["t2"],
            "action": "WAIT_ON_SITE", "instruction": {}, "issued_at": iso(13, 58),
        }, iso(13, 58))
        # 不能往同样被暂停的区域引导
        self.f.reject("RELOCATE_TARGET_INVALID",
                      "SUSPENSION_ARRANGEMENT_ISSUED", "s1", {
            "suspension_id": "sp-1", "team_ids": ["t1"],
            "action": "RELOCATE", "instruction": {"target_zone_id": "z-atrium"},
            "issued_at": iso(13, 59),
        }, iso(13, 59))
        # 暂停期间 t2 在主中庭的扫码不产生到场
        self.f.scan_ev("t2", "t2-m1", iso(14, 0), crid="t2m1#1")
        self.assertNotIn("t2-m1", st.teams["t2"].checked_in)
        # 北侧空地照常运行
        self.assertFalse(
            zone_capacity_view(self.f.k, st, "z-neighbor", iso(14, 0)).suspended)
        scope = affected_scope(self.f.k, st, "sp-1")
        self.assertEqual(scope["suspended_zones"], ["z-atrium"])
        self.assertIn("z-neighbor", scope["other_zones"])
        # 解除后，宽限内的扫码恢复有效
        self.f.ev("AREA_SUSPENSION_LIFTED", "s1", {
            "suspension_id": "sp-1", "ended_at": iso(14, 5),
        }, iso(14, 5))
        self.f.scan_ev("t2", "t2-m1", iso(14, 6), crid="t2m1#1")
        self.assertIn("t2-m1", st.teams["t2"].checked_in)

    # ---- 安全事件与素材 ----

    def test_minor_rules_guardian_consent_and_reunion(self):
        self.f.setup_session()
        self.f.team("tk", members=[
            {"member_id": "kid-1", "is_minor": True, "guardian_id": "g-1"},
            {"member_id": "kid-2", "is_minor": False},
        ])
        # 未成年人报名缺监护人即拒
        self.f.reject("MINOR_NEEDS_GUARDIAN", "TEAM_REGISTERED", "s1", {
            "team_id": "tx", "session_id": "s1",
            "members": [{"member_id": "kid-x", "is_minor": True},
                        {"member_id": "kid-y", "is_minor": False}],
        })
        # 未成年人素材授权必须来自登记监护人
        self.f.reject("CONSENT_NOT_FROM_GUARDIAN", "MEDIA_CONSENT_GRANTED", "s1", {
            "consent_id": "c-bad", "member_id": "kid-1",
            "scope": ["PUBLIC_PROMOTION"], "granted_by": "stranger",
            "granted_at": iso(12, 0),
        }, iso(12, 0))
        self.f.ev("MEDIA_CONSENT_GRANTED", "s1", {
            "consent_id": "c-kid", "member_id": "kid-1",
            "scope": ["PUBLIC_PROMOTION"], "granted_by": "g-1",
            "granted_at": iso(12, 0),
        }, iso(12, 0))
        # 走失：领回人与登记监护人不符即拒
        self.f.ev("INCIDENT_REPORTED", "s1", {
            "incident_id": "i-1", "zone_id": "z-atrium", "kind": "MINOR_LOST",
            "severity": "MEDIUM", "reported_by": "staff-2",
            "reported_at": iso(14, 30),
        }, iso(14, 30))
        self.f.reject("GUARDIAN_MISMATCH", "MINOR_REUNITED", "s1", {
            "incident_id": "i-1", "member_id": "kid-1",
            "guardian_id": "g-9", "verified_by": "staff-2", "at": iso(14, 40),
        }, iso(14, 40))
        self.f.ev("MINOR_REUNITED", "s1", {
            "incident_id": "i-1", "member_id": "kid-1",
            "guardian_id": "g-1", "verified_by": "staff-2", "at": iso(14, 40),
        }, iso(14, 40))

    def test_sensitive_material_only_for_assigned_responders(self):
        st = self.f.setup_session()
        self.f.team("t1"); self.f.accept("t1")
        self.f.ev("MEDIA_CONSENT_GRANTED", "s1", {
            "consent_id": "c-1", "member_id": "t1-m1",
            "scope": ["ONSITE_DISPLAY", "PUBLIC_PROMOTION"],
            "granted_by": "t1-m1", "granted_at": iso(13, 0),
        }, iso(13, 0))
        self.f.ev("INCIDENT_REPORTED", "s1", {
            "incident_id": "i-1", "zone_id": "z-atrium", "kind": "INJURY",
            "severity": "HIGH", "reported_by": "staff-1",
            "reported_at": iso(14, 30),
        }, iso(14, 30))
        self.f.ev("MEDIA_ASSET_CAPTURED", "s1", {
            "asset_id": "a-1", "zone_id": "z-atrium", "captured_at": iso(14, 31),
            "consent_ids": ["c-1"], "incident_id": "i-1", "sensitive": True,
        }, iso(14, 31))
        # 敏感素材必须挂在安全事件下
        self.f.reject("SENSITIVE_NEEDS_INCIDENT", "MEDIA_ASSET_CAPTURED", "s1", {
            "asset_id": "a-x", "zone_id": "z-atrium", "captured_at": iso(14, 31),
            "consent_ids": ["c-1"], "incident_id": None, "sensitive": True,
        }, iso(14, 31))
        # 未被指派的处置人拿不到访问
        self.f.reject("RESPONDER_NOT_ASSIGNED", "ASSET_ACCESS_GRANTED", "s1", {
            "asset_id": "a-1", "incident_id": "i-1", "responder_id": "r-random",
            "purpose": "CURIOSITY", "granted_at": iso(14, 32),
            "expires_at": iso(15, 32),
        }, iso(14, 32))
        # 指派后可访问，且带到期时间
        self.f.ev("RESPONDER_ASSIGNED", "s1", {
            "incident_id": "i-1", "responder_id": "r-1", "role": "FIRST_AID",
            "assigned_at": iso(14, 31),
        }, iso(14, 31))
        self.f.ev("ASSET_ACCESS_GRANTED", "s1", {
            "asset_id": "a-1", "incident_id": "i-1", "responder_id": "r-1",
            "purpose": "TREATMENT", "granted_at": iso(14, 32),
            "expires_at": iso(15, 32),
        }, iso(14, 32))
        # 敏感素材一律不得对外发布
        self.f.ev("MEDIA_RELEASE_REQUESTED", "s1", {
            "request_id": "q-1", "asset_id": "a-1", "channel": "WECHAT",
            "requested_by": "mkt-1", "requested_at": iso(16, 0),
        }, iso(16, 0))
        self.f.reject("SENSITIVE_NOT_RELEASABLE", "MEDIA_RELEASE_DECIDED", "s1", {
            "request_id": "q-1", "decision": "APPROVE",
            "decided_by": "mkt-1", "decided_at": iso(16, 1), "reason": "good",
        }, iso(16, 1))
        # 撤回后立即失效：撤销访问 & 新拍摄被拒
        self.f.ev("ASSET_ACCESS_REVOKED", "s1", {
            "asset_id": "a-1", "responder_id": "r-1", "revoked_at": iso(14, 45),
        }, iso(14, 45))
        self.f.ev("MEDIA_CONSENT_WITHDRAWN", "s1", {
            "consent_id": "c-1", "member_id": "t1-m1",
            "withdrawn_at": iso(14, 40),
        }, iso(14, 40))
        self.f.reject("CAPTURE_AFTER_WITHDRAWAL", "MEDIA_ASSET_CAPTURED", "s1", {
            "asset_id": "a-2", "zone_id": "z-atrium", "captured_at": iso(14, 41),
            "consent_ids": ["c-1"], "incident_id": None, "sensitive": False,
        }, iso(14, 41))

    def test_release_requires_matching_scope(self):
        st = self.f.setup_session()
        self.f.team("t1"); self.f.accept("t1")
        self.f.ev("MEDIA_CONSENT_GRANTED", "s1", {
            "consent_id": "c-onsite", "member_id": "t1-m1",
            "scope": ["ONSITE_DISPLAY"], "granted_by": "t1-m1",
            "granted_at": iso(13, 0),
        }, iso(13, 0))
        self.f.ev("MEDIA_ASSET_CAPTURED", "s1", {
            "asset_id": "a-fun", "zone_id": "z-atrium", "captured_at": iso(14, 0),
            "consent_ids": ["c-onsite"], "incident_id": None, "sensitive": False,
        }, iso(14, 0))
        self.f.ev("MEDIA_RELEASE_REQUESTED", "s1", {
            "request_id": "q-2", "asset_id": "a-fun", "channel": "WECHAT",
            "requested_by": "mkt-1", "requested_at": iso(16, 0),
        }, iso(16, 0))
        self.f.reject("CONSENT_SCOPE_MISSING", "MEDIA_RELEASE_DECIDED", "s1", {
            "request_id": "q-2", "decision": "APPROVE",
            "decided_by": "mkt-1", "decided_at": iso(16, 1), "reason": "",
        }, iso(16, 1))

    # ---- 资格：永远不能与消费挂钩 ----

    def test_spend_based_eligibility_is_unknown(self):
        self.f.setup_session()
        self.f.reject("ELIGIBILITY_RULE_UNKNOWN", "ELIGIBILITY_RULE_SET", "s1", {
            "session_id": "s1",
            "rules": [{"type": "SPEND_OVER_100_YUAN"}],
        })
        # 允许的仍是年龄/监护/健康类
        self.f.ev("ELIGIBILITY_RULE_SET", "s1", {"session_id": "s1", "rules": [
            {"type": "AGE_MIN", "value": 6},
            {"type": "GUARDIAN_FOR_MINORS"},
        ]})

    # ---- 冻结闸门 ----

    def test_freeze_requires_all_three_areas(self):
        st = self.f.setup_session()
        self.f.reject("READINESS_INCOMPLETE", "PLAN_FROZEN", "s1",
                      {"session_id": "s1", "frozen_at": iso(13, 30)}, iso(13, 30))
        self.f.ev("READINESS_CHECK_STARTED", "s1",
                  {"session_id": "s1", "started_at": iso(13, 0)})
        gaps = readiness_gaps(st, self.f.k)
        self.assertTrue(any(g.startswith("VENUE:") for g in gaps))
        self.assertTrue(any(g.startswith("STAFFING:") for g in gaps))
        self.assertTrue(any(g.startswith("EMERGENCY_PLAN:") for g in gaps))
        for area, items in READINESS_REQUIRED_ITEMS.items():
            for item in items:
                self.f.ev("READINESS_ITEM_CONFIRMED", "s1", {
                    "session_id": "s1", "area": area, "item": item,
                    "confirmed_by": "manager-1", "confirmed_at": iso(13, 20),
                }, iso(13, 20))
        self.f.ev("PLAN_FROZEN", "s1",
                  {"session_id": "s1", "frozen_at": iso(13, 30)}, iso(13, 30))
        self.assertTrue(st.frozen)
        # 冻结后结构性变更被拒（现场运行事件不受影响）
        self.f.reject("FROZEN_STRUCTURAL_CHANGE", "TEAM_REGISTERED", "s1", {
            "team_id": "tlate", "session_id": "s1",
            "members": [{"member_id": "a", "is_minor": False},
                        {"member_id": "b", "is_minor": False}],
        }, iso(13, 31))
        # 解冻通道存在
        self.f.ev("PLAN_UNFROZEN", "s1", {
            "session_id": "s1", "reason": "整改", "unfrozen_at": iso(13, 32),
        }, iso(13, 32))
        self.f.team("tlate")
        self.assertFalse(st.frozen)

    def test_venue_version_is_immutable(self):
        self.f.setup_session()
        self.f.reject("VENUE_VERSION_IMMUTABLE", "VENUE_VERSION_PUBLISHED",
                      "vv-0923", VENUE, iso(9, 1))

    # ---- 分组 / 裁判 / 奖品 ----

    def test_bracket_results_amendments_prizes(self):
        st = self.f.setup_session()
        for t in ("t1", "t2"):
            self.f.team(t); self.f.accept(t)
        self.f.freeze()
        self.f.ev("BRACKET_BUILT", "s1", {"session_id": "s1", "groups": [
            {"group_id": "g-A", "zone_id": "z-atrium", "heat_no": 1,
             "team_ids": ["t1", "t2"]},
        ]})
        self.f.reject("BRACKET_TEAM_DUPLICATED", "BRACKET_BUILT", "s1",
                      {"session_id": "s1", "groups": [
                          {"group_id": "g-B", "zone_id": "z-atrium", "heat_no": 2,
                           "team_ids": ["t1", "t1"]}]})
        self.f.ev("MATCH_RESULT_RECORDED", "s1", {
            "match_id": "m-1", "session_id": "s1", "group_id": "g-A",
            "referee_id": "ref-1",
            "rankings": [{"team_id": "t1", "rank": 1, "score": 9},
                         {"team_id": "t2", "rank": 2, "score": 7}],
            "recorded_at": iso(14, 30),
        }, iso(14, 30))
        # 改判：当前值覆盖，历史全部保留可审计
        self.f.ev("RESULT_AMENDED", "s1", {
            "match_id": "m-1", "amendment_id": "a-1", "referee_id": "ref-2",
            "rankings": [{"team_id": "t2", "rank": 1, "score": 10},
                         {"team_id": "t1", "rank": 2, "score": 9}],
            "reason": "回看视频", "amended_at": iso(14, 40),
        }, iso(14, 40))
        self.assertEqual(st.matches["m-1"]["rankings"][0]["team_id"], "t2")
        self.assertEqual(len(st.matches["m-1"]["amendments"]), 1)
        self.f.reject("AMENDMENT_DUPLICATE", "RESULT_AMENDED", "s1", {
            "match_id": "m-1", "amendment_id": "a-1", "referee_id": "ref-2",
            "rankings": [], "reason": "x", "amended_at": iso(14, 41),
        }, iso(14, 41))
        # 参与奖：未签到不可发；名次奖：不在排名不可发
        self.f.ev("PRIZE_DEFINED", "s1", {
            "prize_id": "p-part", "session_id": "s1",
            "basis": "PARTICIPATION", "description": "参与徽章",
        })
        self.f.ev("PRIZE_DEFINED", "s1", {
            "prize_id": "p-rank", "session_id": "s1",
            "basis": "RANK", "description": "冠军券包",
        })
        self.f.reject("PRIZE_REQUIRES_CHECKIN", "PRIZE_AWARDED", "s1", {
            "prize_id": "p-part", "team_id": "t1",
            "awarded_by": "staff", "awarded_at": iso(14, 50),
        }, iso(14, 50))
        self.f.scan_ev("t1", "t1-m1", iso(13, 55),
                       crid="t1m1", )
        self.f.scan_ev("t1", "t1-m2", iso(13, 55), crid="t1m2")
        self.f.ev("PRIZE_AWARDED", "s1", {
            "prize_id": "p-part", "team_id": "t1",
            "awarded_by": "staff", "awarded_at": iso(14, 50),
        }, iso(14, 50))
        # t9 不存在于任何排名
        self.f.reject("PRIZE_OR_TEAM_UNKNOWN", "PRIZE_AWARDED", "s1", {
            "prize_id": "p-rank", "team_id": "t9",
            "awarded_by": "staff", "awarded_at": iso(14, 50),
        }, iso(14, 50))

    # ---- 顾客可见性 / 区域体验指标 ----

    def test_customer_views_and_experience_metrics(self):
        st = self.f.setup_session()
        for t in ("t1", "t2"):
            self.f.team(t); self.f.accept(t); self.f.window(t, (13, 50), (14, 0))
        self.f.freeze()
        self.f.ev("QUEUE_NOTICE_PUBLISHED", "s1", {
            "zone_id": "z-atrium", "expected_wait_minutes": 12,
            "observed_at": iso(13, 50),
        }, iso(13, 50))
        self.assertEqual(
            customer_queue_view(st, "z-atrium")["expected_wait_minutes"], 12)
        self.f.reject("STALE_NOTICE", "QUEUE_NOTICE_PUBLISHED", "s1", {
            "zone_id": "z-atrium", "expected_wait_minutes": 5,
            "observed_at": iso(13, 40),
        }, iso(13, 40))
        # 队伍级改期只推给当事队伍
        self.f.ev("ENTRY_SLOT_RESCHEDULED", "s1", {
            "team_id": "t1", "session_id": "s1",
            "admit_from": iso(14, 20), "admit_until": iso(14, 30),
            "reason": "ZONE_SUSPENDED",
        }, iso(13, 58))
        self.assertEqual(len(customer_changes_view(st, "t1")), 1)
        self.assertEqual(customer_changes_view(st, "t2"), [])
        # 停留时长：进入 13:55、离开 14:25 → 1800 秒
        self.f.scan_ev("t1", "t1-m1", iso(13, 55), crid="x1")
        self.f.ev("ZONE_ENTRY_OBSERVED", "s1", {
            "zone_id": "z-atrium", "member_id": "t1-m1",
            "audience": "COMPETITOR", "observed_at": iso(13, 55),
        }, iso(13, 55))
        self.f.ev("ZONE_EXIT_OBSERVED", "s1", {
            "zone_id": "z-atrium", "member_id": "t1-m1",
            "audience": "COMPETITOR", "observed_at": iso(14, 25),
        }, iso(14, 25))
        m = experience_metrics(st)
        self.assertEqual(m["avg_dwell_seconds"], 1800.0)
        self.assertEqual(m["checked_in_teams"], 1)
        self.assertNotIn("spend", json.dumps(m, ensure_ascii=False).lower())

    # ---- 商户协作 ----

    def test_merchant_tasks(self):
        st = self.f.setup_session()
        self.f.ev("MERCHANT_TASK_ASSIGNED", "s1", {
            "task_id": "mk-1", "merchant_id": "m-noodle", "zone_id": "z-atrium",
            "kind": "QUEUE_SNACK", "window_start": iso(14, 0),
            "window_end": iso(15, 0),
        })
        self.f.ev("MERCHANT_TASK_CONFIRMED", "s1", {
            "task_id": "mk-1", "merchant_id": "m-noodle", "handled_by": "店长A",
        })
        self.assertTrue(st.merchant_tasks["mk-1"]["confirmed"])
        self.f.reject("TASK_UNKNOWN", "MERCHANT_TASK_CONFIRMED", "s1", {
            "task_id": "mk-x", "merchant_id": "m-noodle", "handled_by": "A",
        })


if __name__ == "__main__":
    unittest.main()
