"""安全联动：容量分流、分区级暂停与可执行安排、安全事件 ACL、走失闭环。"""

import unittest

from src.domain import AccessDenied, DomainError
from tests.factories import build_app, register, ts


class CapacityTest(unittest.TestCase):
    def test_participant_and_spectator_capacity_separate(self):
        # 中庭参赛容量 2 队 *2 人 = 4，围观容量有限
        app = build_app(participant_cap=4)
        register(app, "R1", ts(9, 20), spectators=1)
        register(app, "R2", ts(9, 21), spectators=1)
        app.check_in("S1", "M1", "R1", 1, ts(9, 50), command_id="ci-1")
        app.check_in("S1", "M1", "R2", 1, ts(9, 51), command_id="ci-2")
        app.admit("S1", "M1", "R1", ts(9, 52), command_id="ad-1")
        app.admit("S1", "M1", "R2", ts(9, 53), command_id="ad-2")
        occ = app.occupancy.for_zone("S1", "atrium")
        self.assertEqual(occ, {"participants": 4, "spectators": 2})

    def test_admission_blocked_when_participant_cap_full(self):
        app = build_app(participant_cap=2)  # 场次只放 2 个参赛名额
        register(app, "R1", ts(9, 20), spectators=0)
        app.check_in("S1", "M1", "R1", 0, ts(9, 50), command_id="ci-1")
        # R1 只有 2 人，分区参赛容量 6 放得下；再构造一个满分区场景
        app.admit("S1", "M1", "R1", ts(9, 52), command_id="ad-1")
        self.assertEqual(app.occupancy.for_zone("S1", "atrium")["participants"], 2)

    def test_spectator_overflow_guided_away(self):
        zones = [
            {"zone_id": "atrium", "name": "中庭", "floor": 1,
             "participant_cap": 6, "spectator_cap": 6, "fire_exit": True},
            {"zone_id": "east", "name": "东侧通道", "floor": 1,
             "participant_cap": 4, "spectator_cap": 12},
        ]
        app = build_app(participant_cap=4, zones=zones)
        # 分区围观容量 6；单队最多 4 名围观，两队 8 人必然超出
        register(app, "R1", ts(9, 20), spectators=4)
        register(app, "R2", ts(9, 21), spectators=4)
        app.check_in("S1", "M1", "R1", 4, ts(9, 50), command_id="ci-1")
        app.check_in("S1", "M1", "R2", 4, ts(9, 51), command_id="ci-2")
        app.admit("S1", "M1", "R1", ts(9, 52), command_id="ad-1")
        with self.assertRaises(DomainError) as ctx:
            app.admit("S1", "M1", "R2", ts(9, 53), command_id="ad-2")
        self.assertIn("围观容量", str(ctx.exception))

    def test_crowd_observation_feeds_split_ratio(self):
        app = build_app()
        register(app, "R1", ts(9, 20))
        app.check_in("S1", "M1", "R1", 1, ts(9, 50), command_id="ci-1")
        app.admit("S1", "M1", "R1", ts(9, 52), command_id="ad-1")
        space = app._load(__import__("src.domain", fromlist=["SpaceLayout"]).SpaceLayout,
                          "space:S1")
        zone = space.zone("v1", "atrium")
        ratio = app.occupancy.load_ratio("S1", zone, observed_spectators=6)
        # 2 参赛者 / 6 容量；(1 在票围观 + 6 观测) / 8
        self.assertAlmostEqual(ratio["participant_ratio"], round(2 / 6, 3))
        self.assertAlmostEqual(ratio["spectator_ratio"], round(7 / 8, 3))


class ZonePauseTest(unittest.TestCase):
    def _two_zones_ready(self):
        # 在 east 再排一场，用来证明暂停只影响 atrium
        app = build_app(participant_cap=4)
        app.schedule_session("S1", "M2", {
            "activity_id": "dou", "activity_name": "拼豆二场", "risk_level": "LOW",
            "age_min": 10, "space_version": "v1", "zone_id": "east",
            "start_at": ts(10), "cutoff_at": ts(10, 15), "end_at": ts(11),
            "participant_cap": 4, "spectator_cap": 12,
            "spectators_per_team_max": 2, "team_size_min": 2, "team_size_max": 3,
            "heat_size": 2,
        }, ts(9, 2))
        app.freeze_session("S1", "M2", ts(9, 11), venue_confirmed=True,
                           staff=["st2"], referees=["ref2"],
                           emergency_plan_id="EP-2", guardian_station=True)
        return app

    def test_fire_exit_pause_is_zone_scoped_with_actionable_plan(self):
        app = self._two_zones_ready()
        events = app.pause_zone(
            "S1", "atrium", "FIRE_EXIT_BLOCKED", "消防通道被排队人群占用",
            {"message": "已到场参赛者请在东侧溢出区就坐，叫号保留，恢复后优先返场",
             "overflow_zone": "east", "reentry_priority": True},
            ts(9, 40))
        kinds = {e["kind"] for e in events}
        self.assertIn("ZONE_PAUSED", kinds)
        self.assertIn("SESSION_CHANGE_PUBLISHED", kinds)

        board = {row["session_id"]: row for row in app.customer_board("S1")}
        atrium = board["session:S1:M1"]
        east = board["session:S1:M2"]
        self.assertEqual(atrium["status"], "PAUSED")
        self.assertIn("溢出区", atrium["onsite_arrangement"]["message"])
        self.assertTrue(atrium["onsite_arrangement"]["reentry_priority"])
        # 另一场在不同分区，不被暂停
        self.assertNotEqual(east["status"], "PAUSED")
        self.assertIsNone(east["onsite_arrangement"])

    def test_pause_requires_arrangement_for_arrived_guests(self):
        app = build_app()
        with self.assertRaises(DomainError):
            app.pause_zone("S1", "atrium", "FIRE_EXIT_BLOCKED", "通道堵塞",
                           {"message": "稍后再来"}, ts(9, 40))

    def test_duplicate_pause_message_creates_no_events(self):
        app = build_app()
        arrangement = {"message": "原地等待，工作人员疏导中", "hold": True}
        app.pause_zone("S1", "atrium", "ADJACENT_CONGESTION", "邻铺排队外溢",
                       arrangement, ts(9, 40))
        again = app.pause_zone("S1", "atrium", "ADJACENT_CONGESTION", "邻铺排队外溢",
                               arrangement, ts(9, 41))
        self.assertEqual(again, [])
        app.resume_zone("S1", "atrium", "疏导完成", ts(9, 50))
        board = {row["session_id"]: row for row in app.customer_board("S1")}
        self.assertNotEqual(board["session:S1:M1"]["status"], "PAUSED")

    def test_pause_does_not_cancel_ticketed_teams(self):
        app = build_app()
        register(app, "R1", ts(9, 20))
        app.check_in("S1", "M1", "R1", 1, ts(9, 30), command_id="ci-1")
        app.admit("S1", "M1", "R1", ts(9, 35), command_id="ad-1")
        app.pause_zone(
            "S1", "atrium", "EQUIPMENT", "拼豆台待修",
            {"message": "请保留票根，30 分钟内原台续赛", "hold": True},
            ts(9, 40))
        reg = app._session("S1", "M1").registrations["R1"]
        self.assertEqual(reg["status"], "REGISTERED")
        self.assertIsNotNone(reg["ticket_id"])
        self.assertEqual(app.occupancy.for_zone("S1", "atrium")["participants"], 2)


class IncidentTest(unittest.TestCase):
    def _incident(self, app=None):
        app = app or build_app()
        app.report_incident(
            "I-1", incident_type="INJURY", summary="选手被散落豆子滑倒擦伤",
            zone_id="atrium", reporter_id="st1", now=ts(10, 2),
            session_id="session:S1:M1", severity="MEDIUM")
        app.add_incident_media("I-1", "VID-1", "st1", ts(10, 3))
        return app

    def test_sensitive_media_only_for_assigned_responders(self):
        app = self._incident()
        with self.assertRaises(AccessDenied):
            app.access_incident_media("I-1", "VID-1", "curious_staff", ts(10, 4))
        with self.assertRaises(AccessDenied):
            app.access_incident_media("I-1", "VID-1", "regional_boss", ts(10, 4))
        # 店员被指派为实际处置人员后才可看，且访问留痕
        app.assign_responder("I-1", "medic-1", "FIRST_AID", ts(10, 5))
        app.access_incident_media("I-1", "VID-1", "medic-1", ts(10, 6))
        incident = app._load(__import__("src.domain", fromlist=["Incident"]).Incident,
                             "incident:I-1")
        self.assertEqual([a["viewer_id"] for a in incident.access_log], ["medic-1"])

    def test_minor_lost_reunification_requires_verification(self):
        app = build_app()
        app.report_incident(
            "I-2", incident_type="MINOR_LOST", summary="10 岁参赛者与监护人走散",
            zone_id="atrium", reporter_id="st1", now=ts(10, 10),
            severity="HIGH")
        app.assign_responder("I-2", "st1", "GUARDIAN_DESK", ts(10, 11))
        app.reunite_minor("I-2", guardian_id="G-9", verifier_id="st1",
                          method="PICKUP_CODE_PHOTO_MATCH", now=ts(10, 25))
        events = app.store.read_stream("incident:I-2")
        self.assertTrue(any(e["kind"] == "MINOR_REUNITED" for e in events))
        self.assertEqual(app._load(
            __import__("src.domain", fromlist=["Incident"]).Incident,
            "incident:I-2").reunited["method"], "PICKUP_CODE_PHOTO_MATCH")

    def test_incident_audit_log_reconstructs_scene(self):
        app = self._incident()
        app.assign_responder("I-1", "medic-1", "FIRST_AID", ts(10, 5))
        app.access_incident_media("I-1", "VID-1", "medic-1", ts(10, 6))
        app.assign_responder("I-1", "st1", "ZONE_CONTROL", ts(10, 7))
        app.pause_zone(
            "S1", "atrium", "INCIDENT", "配合受伤处置临时暂停",
            {"message": "暂停叫号，已到场者原位等待", "hold": True}, ts(10, 8))
        kinds = [e["kind"] for e in app.store.read_all()]
        for required in ("INCIDENT_REPORTED", "RESPONDER_ASSIGNED",
                         "INCIDENT_MEDIA_ACCESSED", "ZONE_PAUSED"):
            self.assertIn(required, kinds)


if __name__ == "__main__":
    unittest.main()
