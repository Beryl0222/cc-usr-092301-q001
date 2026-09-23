"""名额与幂等：重复扫码、断网补签、迟到取消、缺席清理都不能制造名额。"""

import unittest

from src.domain import DomainError
from tests.factories import adult, build_app, register, ts


class RosterTest(unittest.TestCase):
    def test_capacity_and_fifo_waitlist(self):
        app = build_app(participant_cap=2)
        register(app, "R1", ts(9, 20))
        register(app, "R2", ts(9, 21))
        register(app, "R3", ts(9, 22))  # 满员进候补
        q = app.session_queue("S1", "M1", now=ts(9, 23))
        self.assertEqual(q["open_slots"], 0)
        self.assertEqual(q["waitlist_len"], 1)

        # 截止前退出：队首 R3 按 FIFO 递补，仍无空位
        app.cancel_registration("S1", "M1", "R1", ts(9, 30), command_id="cx-1")
        q = app.session_queue("S1", "M1", now=ts(9, 31))
        self.assertEqual(q["waitlist_len"], 0)
        self.assertEqual(q["open_slots"], 0)
        agg = app._session("S1", "M1")
        self.assertEqual(agg.registrations["R3"]["status"], "REGISTERED")

    def test_duplicate_scan_is_idempotent(self):
        app = build_app(participant_cap=2)
        register(app, "R1", ts(9, 20), command_id="scan-1")
        stream_before = len(app.store.read_stream("session:S1:M1"))
        # 顾客网络抖动连点两次：同一 command_id 重放
        second = register(app, "R1", ts(9, 20), command_id="scan-1")
        self.assertEqual(second, [])
        self.assertEqual(len(app.store.read_stream("session:S1:M1")), stream_before)
        self.assertEqual(app.session_queue("S1", "M1", now=ts(9, 21))["open_slots"], 1)

    def test_offline_checkin_replay_does_not_double_count(self):
        app = build_app(participant_cap=2)
        register(app, "R1", ts(9, 20))
        app.check_in("S1", "M1", "R1", 1, ts(9, 50), command_id="ci-1")
        # 终端断网后本地重试同一条签到
        app.check_in("S1", "M1", "R1", 1, ts(9, 51), command_id="ci-1")
        app.admit("S1", "M1", "R1", ts(9, 55), command_id="ad-1")
        # 闸机同样重放
        app.admit("S1", "M1", "R1", ts(9, 56), command_id="ad-1")
        self.assertEqual(app.occupancy.for_zone("S1", "atrium"),
                         {"participants": 2, "spectators": 1})

    def test_late_cancel_creates_no_slot(self):
        app = build_app(participant_cap=2)
        register(app, "R1", ts(9, 20))
        register(app, "R2", ts(9, 21))
        register(app, "R3", ts(9, 22))
        app.cancel_registration("S1", "M1", "R1", ts(9, 30))  # R3 递补
        # R3 已获名额但没来：10:15 后取消，不允许再递补
        events = app.cancel_registration("S1", "M1", "R3", ts(10, 20))
        kinds = [e["kind"] for e in events]
        self.assertIn("REGISTRATION_CANCELLED", kinds)
        self.assertNotIn("WAITLIST_PROMOTED", kinds)
        q = app.session_queue("S1", "M1", now=ts(10, 21))
        self.assertFalse(q["accepting"])
        self.assertEqual(q["open_slots"], 0)  # 对顾客显示不可订，而非“还剩 1 个”

    def test_duplicate_late_cancel_is_silently_idempotent(self):
        app = build_app()
        register(app, "R1", ts(9, 20))
        app.cancel_registration("S1", "M1", "R1", ts(10, 20), command_id="cx-1")
        again = app.cancel_registration("S1", "M1", "R1", ts(10, 21), command_id="cx-1")
        self.assertEqual(again, [])

    def test_no_show_sweep_after_cutoff_promotes_nobody(self):
        app = build_app(participant_cap=2)
        register(app, "R1", ts(9, 20))
        register(app, "R2", ts(9, 21))
        register(app, "R3", ts(9, 22))  # 候补
        with self.assertRaises(DomainError):
            app.sweep_no_shows("S1", "M1", ts(10, 14))
        events = app.sweep_no_shows("S1", "M1", ts(10, 16))
        kinds = {e["kind"] for e in events}
        self.assertIn("NO_SHOW_CANCELLED", kinds)
        self.assertNotIn("WAITLIST_PROMOTED", kinds)
        # 候补者在截止后也不能被“补签”进来
        with self.assertRaises(DomainError):
            app.check_in("S1", "M1", "R3", 0, ts(10, 17), command_id="late-ci")
        with self.assertRaises(DomainError):
            register(app, "R4", ts(10, 18), command_id="walkup")

    def test_ticket_released_on_cancel_frees_real_occupancy(self):
        app = build_app(participant_cap=2)
        register(app, "R1", ts(9, 20))
        app.check_in("S1", "M1", "R1", 1, ts(9, 50), command_id="ci-1")
        app.admit("S1", "M1", "R1", ts(9, 55), command_id="ad-1")
        self.assertEqual(app.occupancy.for_zone("S1", "atrium")["participants"], 2)
        app.cancel_registration("S1", "M1", "R1", ts(9, 57))
        self.assertEqual(app.occupancy.for_zone("S1", "atrium"),
                         {"participants": 0, "spectators": 0})

    def test_store_rejects_bad_version(self):
        app = build_app()
        register(app, "R1", ts(9, 20))
        from src.store import ConcurrentStreamError
        bad = {
            "event_id": "x", "kind": "TEAM_REGISTERED",
            "occurred_at": ts(9, 21), "subject_id": "session:S1:M1",
            "version": 99, "data": {},
        }
        with self.assertRaises(ConcurrentStreamError):
            app.store.append(bad)

    def test_state_rebuilds_from_event_log(self):
        app = build_app(participant_cap=2)
        register(app, "R1", ts(9, 20))
        register(app, "R2", ts(9, 21))
        register(app, "R3", ts(9, 22))
        app.check_in("S1", "M1", "R1", 1, ts(9, 50), command_id="ci-1")
        app.admit("S1", "M1", "R1", ts(9, 55), command_id="ad-1")
        # 用同一个事件日志开一个新进程视角，状态应完全一致
        rebuilt = type(app)(app.store)
        self.assertEqual(rebuilt.occupancy.for_zone("S1", "atrium"),
                         app.occupancy.for_zone("S1", "atrium"))
        self.assertEqual(
            rebuilt.session_queue("S1", "M1", now=ts(9, 56)),
            app.session_queue("S1", "M1", now=ts(9, 56)),
        )


if __name__ == "__main__":
    unittest.main()
