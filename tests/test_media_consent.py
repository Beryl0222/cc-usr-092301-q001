"""拍摄授权：未成年人监护授权、撤回即时生效、事件敏感材料、下架闭环。"""

import unittest

from src.domain import AccessDenied, DomainError
from tests.factories import build_app, ts


class ConsentTest(unittest.TestCase):
    def test_minor_grant_requires_guardian(self):
        app = build_app()
        with self.assertRaises(DomainError):
            app.grant_consent("KID1", ["PHOTO"], ts(9, 20), minor=True)
        app.grant_consent("KID1", ["PHOTO"], ts(9, 21),
                          minor=True, guardian_id="G1")

    def test_no_consent_blocks_publication(self):
        app = build_app()
        app.capture_media("IMG-1", "session:S1:M1", "atrium", ts(10, 5),
                          ["P1", "P2"], "PHOTO", ts(10, 6))
        decision = app.publish_decision("IMG-1", ts(10, 7))
        self.assertFalse(decision["allowed"])
        self.assertTrue(any("NO_CONSENT" for r in decision["reasons"]))

    def test_withdrawal_takes_effect_immediately_even_after_capture(self):
        app = build_app()
        app.grant_consent("P1", ["PHOTO"], ts(9, 20))
        app.grant_consent("KID1", ["PHOTO"], ts(9, 21),
                          minor=True, guardian_id="G1")
        # 拍摄时两人都有授权，素材合法采集
        app.capture_media("IMG-1", "session:S1:M1", "atrium", ts(10, 5),
                          ["P1", "KID1"], "PHOTO", ts(10, 6))
        self.assertTrue(app.publish_decision("IMG-1", ts(10, 7))["allowed"])
        # 监护人撤回：发布即刻被拦截，尽管撤回发生在拍摄之后
        app.withdraw_consent("KID1", ["PHOTO"], ts(10, 8))
        decision = app.block_if_unlicensed("IMG-1", ts(10, 9))
        self.assertFalse(decision["allowed"])
        self.assertIn("MINOR_CONSENT_WITHDRAWN:KID1", decision["reasons"])
        media = app._load(__import__("src.domain", fromlist=["MediaAsset"]).MediaAsset,
                          "media:IMG-1")
        self.assertTrue(media.block_reasons)

    def test_regrant_after_withdrawal_reopens(self):
        app = build_app()
        app.grant_consent("P1", ["PHOTO"], ts(9, 20))
        app.capture_media("IMG-1", "session:S1:M1", "atrium", ts(10, 5),
                          ["P1"], "PHOTO", ts(10, 6))
        app.withdraw_consent("P1", ["PHOTO"], ts(10, 8))
        self.assertFalse(app.publish_decision("IMG-1", ts(10, 9))["allowed"])
        app.grant_consent("P1", ["PHOTO"], ts(10, 20))
        self.assertTrue(app.publish_decision("IMG-1", ts(10, 21))["allowed"])

    def test_scope_specific(self):
        app = build_app()
        app.grant_consent("P1", ["PHOTO"], ts(9, 20))
        app.capture_media("VID-1", "session:S1:M1", "atrium", ts(10, 5),
                          ["P1"], "VIDEO", ts(10, 6))
        decision = app.publish_decision("VID-1", ts(10, 7))
        self.assertFalse(decision["allowed"])
        self.assertIn("NO_CONSENT:P1", decision["reasons"])

    def test_takedown_flow(self):
        app = build_app()
        app.grant_consent("P1", ["PHOTO"], ts(9, 20))
        app.capture_media("IMG-1", "session:S1:M1", "atrium", ts(10, 5),
                          ["P1"], "PHOTO", ts(10, 6))
        app.request_takedown("IMG-1", "视频传播引发异议", ts(11, 0))
        app.confirm_takedown("IMG-1", "ops-1", ts(11, 5))
        decision = app.publish_decision("IMG-1", ts(11, 6))
        self.assertFalse(decision["allowed"])
        self.assertIn("ALREADY_TAKEN_DOWN", decision["reasons"])

    def test_incident_media_never_publishable(self):
        app = build_app()
        app.grant_consent("P1", ["PHOTO"], ts(9, 20))
        app.report_incident("I-9", incident_type="INJURY", summary="擦伤",
                            zone_id="atrium", reporter_id="st1", now=ts(10, 2))
        app.capture_media("IMG-9", "session:S1:M1", "atrium", ts(10, 5),
                          ["P1"], "PHOTO", ts(10, 6),
                          sensitive=True, incident_id="incident:I-9")
        decision = app.publish_decision("IMG-9", ts(10, 7))
        self.assertFalse(decision["allowed"])
        self.assertIn("SENSITIVE_INCIDENT_MATERIAL", decision["reasons"])
        # 即便发布被拦，处置人员仍可在内网查看，无关人员不行
        app.add_incident_media("I-9", "m-1", "st1", ts(10, 8))
        with self.assertRaises(AccessDenied):
            app.access_incident_media("I-9", "m-1", "outsider", ts(10, 9))
        app.assign_responder("I-9", "medic-1", "FIRST_AID", ts(10, 10))
        app.access_incident_media("I-9", "m-1", "medic-1", ts(10, 11))


if __name__ == "__main__":
    unittest.main()
