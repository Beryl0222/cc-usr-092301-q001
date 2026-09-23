"""资格与活动前检查：与消费无关、未成年人保护、冻结门槛。"""

import unittest

from src.app import Application
from src.domain import DomainError
from tests.factories import adult, build_app, minor, register, ts


class EligibilityTest(unittest.TestCase):
    def test_spend_related_fields_are_rejected(self):
        app = build_app()
        rich = [
            {"age": 30, "safety_notice_signed": True, "annual_spend": 50000},
            adult(),
        ]
        with self.assertRaises(DomainError) as ctx:
            register(app, "VIP", ts(9, 20), profiles=rich, command_id="vip-1")
        self.assertIn("annual_spend", str(ctx.exception))

        # 会员等级、积分、消费券同样不进入资格判断
        for forbidden in ("membership_tier", "points", "voucher_amount"):
            with self.assertRaises(DomainError):
                register(app, forbidden, ts(9, 21),
                         profiles=[{"age": 30, "safety_notice_signed": True,
                                    forbidden: 1}, adult()],
                         command_id=f"cmd-{forbidden}")

    def test_accepts_only_safety_relevant_attributes(self):
        app = build_app()
        register(app, "OK", ts(9, 20),
                 profiles=[{"age": 40, "guardian_present": False,
                            "safety_notice_signed": True}, adult()])
        self.assertEqual(app.session_queue("S1", "M1", now=ts(9, 21))["open_slots"], 3)

    def test_age_requirement_enforced(self):
        app = build_app(age_min=16)
        with self.assertRaises(DomainError):
            register(app, "KIDS", ts(9, 20),
                     profiles=[minor(12), adult()], command_id="kids")

    def test_minor_needs_guardian_and_notice(self):
        app = build_app()
        with self.assertRaises(DomainError):
            register(app, "NOG", ts(9, 20),
                     profiles=[minor(12, guardian=False), adult()], command_id="nog")
        with self.assertRaises(DomainError):
            register(app, "NOSIGN", ts(9, 20),
                     profiles=[minor(12, signed=False), adult()], command_id="nosign")
        # 监护人在场且签告知即可
        register(app, "FINE", ts(9, 22),
                 profiles=[minor(12), adult()])

    def test_spectators_capped_per_team(self):
        app = build_app()
        with self.assertRaises(DomainError):
            register(app, "CROWD", ts(9, 20), spectators=5, command_id="crowd")

    def test_high_risk_requires_protection_in_catalog(self):
        app = Application()
        with self.assertRaises(DomainError):
            app.register_activity(
                activity_id="slipper", name="甩拖鞋", gameplay="甩远",
                risk_level="HIGH", team_size_min=1, team_size_max=2,
                age_min=18, safety_requirements=(), now=ts(8))
        app.register_activity(
            activity_id="slipper", name="甩拖鞋", gameplay="甩远",
            risk_level="HIGH", team_size_min=1, team_size_max=2,
            age_min=18, safety_requirements=["防滑地垫", "抛掷隔离线"],
            now=ts(8, 1))

    def test_cannot_signup_before_freeze(self):
        app = Application()
        app.draft_space("S1", "v1", "布局",
                        [{"zone_id": "z", "name": "z", "floor": 1,
                          "participant_cap": 10, "spectator_cap": 10}],
                        ts(8), ts(8))
        app.publish_space("S1", "v1", ts(8, 5))
        app.register_activity(activity_id="a", name="嗑瓜子", gameplay="计时",
                              risk_level="LOW", team_size_min=1, team_size_max=2,
                              age_min=18, now=ts(8, 10))
        app.schedule_session("S1", "X", {
            "activity_id": "a", "activity_name": "嗑瓜子", "risk_level": "LOW",
            "age_min": 18, "space_version": "v1", "zone_id": "z",
            "start_at": ts(10), "cutoff_at": ts(10, 15), "end_at": ts(11),
            "participant_cap": 4, "spectator_cap": 10,
            "spectators_per_team_max": 1, "team_size_min": 1, "team_size_max": 2,
            "heat_size": 2,
        }, ts(9))
        # 场地/人员/预案未齐备：报名不允许开放
        with self.assertRaises(DomainError):
            app.open_signup("S1", "X", ts(9, 5))
        with self.assertRaises(DomainError):
            app.freeze_session(
                "S1", "X", ts(9, 6), venue_confirmed=False,
                staff=["st"], referees=["ref"], emergency_plan_id="EP")

    def test_high_risk_session_needs_two_referees_and_staff(self):
        app = build_app()  # 基础 LOW 场次正常冻结
        app.register_activity(
            activity_id="slip", name="甩拖鞋", gameplay="甩远",
            risk_level="HIGH", team_size_min=2, team_size_max=2, age_min=18,
            safety_requirements=["防滑地垫"], now=ts(8, 20))
        app.schedule_session("S1", "H1", {
            "activity_id": "slip", "activity_name": "甩拖鞋", "risk_level": "HIGH",
            "age_min": 18, "space_version": "v1", "zone_id": "atrium",
            "start_at": ts(10), "cutoff_at": ts(10, 15), "end_at": ts(11),
            "participant_cap": 4, "spectator_cap": 8,
            "spectators_per_team_max": 2, "team_size_min": 2, "team_size_max": 2,
            "heat_size": 2,
        }, ts(9, 2))
        with self.assertRaises(DomainError) as ctx:
            app.freeze_session(
                "S1", "H1", ts(9, 11), venue_confirmed=True,
                staff=["st1"], referees=["ref1"], emergency_plan_id="EP-1")
        self.assertIn("安全人员", str(ctx.exception))
        app.freeze_session(
            "S1", "H1", ts(9, 12), venue_confirmed=True,
            staff=["st1", "st2"], referees=["ref1", "ref2"],
            emergency_plan_id="EP-1")


if __name__ == "__main__":
    unittest.main()
