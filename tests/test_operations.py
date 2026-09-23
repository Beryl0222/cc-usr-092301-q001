"""现场运行端到端：分组抽签、裁判记录、按名次发奖、商户协作、体验对比、看板真实性。"""

import unittest

from src.domain import DomainError
from tests.factories import adult, build_app, register, ts


class TournamentFlowTest(unittest.TestCase):
    def _ready_checked_in(self, participant_cap=4):
        app = build_app(participant_cap=participant_cap)
        register(app, "R1", ts(9, 20))
        register(app, "R2", ts(9, 21))
        app.check_in("S1", "M1", "R1", 1, ts(9, 50), command_id="ci-1")
        app.check_in("S1", "M1", "R2", 1, ts(9, 51), command_id="ci-2")
        return app

    def test_groups_only_from_checked_in_teams(self):
        app = self._ready_checked_in()
        register(app, "R3", ts(9, 22))  # 未签到
        events = app.draw_groups("S1", "M1", ts(10, 16))
        grouped = {tid for g in events[0]["data"]["groups"] for tid in g["team_ids"]}
        self.assertEqual(grouped, {"R1", "R2"})
        with self.assertRaises(DomainError):
            app.draw_groups("S1", "M1", ts(10, 17))

    def test_referee_records_and_prizes_follow_ranking(self):
        app = self._ready_checked_in()
        app.draw_groups("S1", "M1", ts(10, 16))
        # 非本场裁判不能记分
        with self.assertRaises(DomainError):
            app.record_result("S1", "M1", "G1",
                              {"R1": 10, "R2": 8}, "ref-other", ts(10, 30))
        app.record_result("S1", "M1", "G1",
                          {"R1": 10, "R2": 8}, "ref1", ts(10, 30))
        app.finish_session("S1", "M1", ts(10, 45))
        agg = app._session("S1", "M1")
        self.assertEqual(agg.final_ranking, ["R1", "R2"])
        # 名次对不上不能发奖（与消费无关，只认成绩）
        with self.assertRaises(DomainError):
            app.award_prize("S1", "M1", "R2", "代金券", 1, ts(10, 50))
        app.award_prize("S1", "M1", "R1", "代金券", 1, ts(10, 51))
        with self.assertRaises(DomainError):
            app.award_prize("S1", "M1", "R1", "代金券", 1, ts(10, 52))

    def test_merchant_collaboration_lifecycle(self):
        app = build_app()
        app.create_merchant_task(
            "B1", task_id="T1", merchant_id="M-Noodle",
            task_type="QUEUE_SUPPORT", description="活动期间错峰出餐，提供排队引导",
            due_at=ts(10), session_id="session:S1:M1", now=ts(9, 5))
        app.update_merchant_task("B1", "T1", "ACCEPTED", "收到", ts(9, 8))
        app.update_merchant_task("B1", "T1", "DONE", "引导岗就位并完成", ts(10, 30))
        board = app._load(__import__("src.domain", fromlist=["MerchantBoard"]).MerchantBoard,
                          "board:B1")
        self.assertEqual(board.tasks["T1"]["status"], "DONE")

    def test_experience_comparison_has_no_spend_dimension(self):
        app = build_app()
        app.sample_experience("S1", "M1", 5, 48, "exit-survey", ts(11, 5))
        app.sample_experience("S1", "M1", 4, 35, "exit-survey", ts(11, 6))
        summary = app.compare_experience(["session:S1:M1"])["session:S1:M1"]
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["avg_rating"], 4.5)
        self.assertNotIn("spend", summary)
        # 评分越界直接拒绝
        with self.assertRaises(DomainError):
            app.sample_experience("S1", "M1", 6, -1, "bad", ts(11, 8))


class CustomerBoardTest(unittest.TestCase):
    def test_board_reflects_real_queue_and_changes(self):
        app = build_app(participant_cap=2)
        register(app, "R1", ts(9, 20))
        register(app, "R2", ts(9, 21))
        register(app, "R3", ts(9, 22))  # 候补
        rows = app.customer_board("S1", now=ts(9, 23))
        row = rows[0]
        self.assertEqual(row["activity_name"], "拼豆")
        self.assertEqual(row["open_slots"], 0)
        self.assertEqual(row["waitlist_len"], 1)
        self.assertIsNone(row["latest_notice"])

        app.publish_change("S1", "M1", "DELAY", "开赛延后 10 分钟", ts(9, 40))
        row = app.customer_board("S1", now=ts(9, 41))[0]
        self.assertEqual(row["latest_notice"]["message"], "开赛延后 10 分钟")

    def test_board_truthful_after_cutoff(self):
        app = build_app(participant_cap=2)
        register(app, "R1", ts(9, 20))
        # R2 缺席，截止后清理
        app.sweep_no_shows("S1", "M1", ts(10, 16))
        row = app.customer_board("S1", now=ts(10, 17))[0]
        self.assertFalse(row["accepting"])
        self.assertEqual(row["open_slots"], 0)  # 不能显示成“可报名额”

    def test_events_replay_rebuilds_board(self):
        app = build_app()
        register(app, "R1", ts(9, 20))
        app.publish_change("S1", "M1", "DELAY", "延后", ts(9, 40))
        app.pause_zone("S1", "atrium", "ADJACENT_CONGESTION", "邻铺拥堵",
                       {"message": "原地等待，叫号保留", "hold": True}, ts(9, 41))
        rebuilt = type(app)(app.store)
        before = app.customer_board("S1", now=ts(9, 42))
        after = rebuilt.customer_board("S1", now=ts(9, 42))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
