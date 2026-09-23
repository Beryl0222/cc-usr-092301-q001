"""合同一致性：领域实际产生的每个事件类型都必须已在 schema 词表中登记，
信封必填字段也必须齐备。"""

import json
import unittest
from pathlib import Path

from src.contract import validate_envelope
from tests.factories import build_app, register, ts

ROOT = Path(__file__).parents[1]


class SchemaVocabularyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "contracts" / "event.schema.json").read_text())
        cls.allowed = set(cls.schema["$defs"]["kind"]["enum"])

    def test_all_emitted_kinds_are_registered(self):
        app = build_app(participant_cap=2)
        register(app, "R1", ts(9, 20))
        register(app, "R2", ts(9, 21))
        register(app, "R3", ts(9, 22))  # 候补
        app.cancel_registration("S1", "M1", "R1", ts(9, 30))  # FIFO 递补
        app.check_in("S1", "M1", "R2", 0, ts(9, 45), command_id="ci2")
        app.admit("S1", "M1", "R2", ts(9, 50), command_id="ad2")
        app.check_in("S1", "M1", "R3", 0, ts(9, 51), command_id="ci")
        app.observe_crowd("S1", "atrium", 2, 3, "staff-count", ts(9, 55))
        app.pause_zone("S1", "atrium", "FIRE_EXIT_BLOCKED", "通道占用",
                       {"message": "原地等待", "hold": True}, ts(9, 56))
        app.resume_zone("S1", "atrium", "恢复", ts(10, 5))
        app.draw_groups("S1", "M1", ts(10, 16))
        app.record_result("S1", "M1", "G1", {"R3": 10, "R2": 8}, "ref1", ts(10, 30))
        app.finish_session("S1", "M1", ts(10, 45))
        app.award_prize("S1", "M1", "R3", "代金券", 1, ts(10, 50))
        app.publish_change("S1", "M1", "DELAY", "延后", ts(9, 40))
        app.sweep_no_shows("S1", "M1", ts(10, 16))

        app.grant_consent("P1", ["PHOTO"], ts(9, 20))
        app.withdraw_consent("P1", ["PHOTO"], ts(9, 25))
        app.capture_media("IMG1", "session:S1:M1", "atrium", ts(10, 2),
                          ["P1"], "PHOTO", ts(10, 3))
        app.block_if_unlicensed("IMG1", ts(10, 4))

        app.report_incident("I1", incident_type="INJURY", summary="擦伤",
                            zone_id="atrium", reporter_id="st1", now=ts(10, 6))
        app.assign_responder("I1", "medic1", "FIRST_AID", ts(10, 7))
        app.add_incident_media("I1", "m1", "st1", ts(10, 8))
        app.access_incident_media("I1", "m1", "medic1", ts(10, 9))
        app.create_merchant_task("B1", task_id="T1", merchant_id="M1",
                                 task_type="QUEUE_SUPPORT", description="引导",
                                 due_at=ts(10), session_id="session:S1:M1",
                                 now=ts(9, 5))
        app.update_merchant_task("B1", "T1", "DONE", "完成", ts(10, 40))
        app.sample_experience("S1", "M1", 5, 40, "survey", ts(11))

        kinds = {e["kind"] for e in app.store.read_all()}
        missing = kinds - self.allowed
        self.assertFalse(missing, f"未登记的事件类型: {sorted(missing)}")

    def test_every_event_has_valid_envelope(self):
        app = build_app()
        register(app, "R1", ts(9, 20))
        for event in app.store.read_all():
            self.assertEqual(validate_envelope(event), [], event)

    def test_fixture_conforms(self):
        sample = json.loads((ROOT / "fixtures" / "event.json").read_text())
        self.assertEqual(validate_envelope(sample), [])
        self.assertIn(sample["kind"], self.allowed)

    def test_schema_is_valid_json(self):
        self.assertEqual(self.schema["type"], "object")
        self.assertIn("SESSION_FROZEN", self.allowed)


if __name__ == "__main__":
    unittest.main()
