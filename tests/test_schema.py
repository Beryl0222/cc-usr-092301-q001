import json
import unittest
from pathlib import Path

from src.events import KINDS
from src.schema import render

ROOT = Path(__file__).parents[1]


class SchemaTest(unittest.TestCase):
    def test_schema_file_is_in_sync_with_catalog(self):
        on_disk = (ROOT / "contracts" / "events.schema.json").read_text(encoding="utf-8")
        self.assertEqual(
            json.loads(on_disk), json.loads(render()),
            "contracts/events.schema.json 已过期，请运行 python -m src.schema 重新生成",
        )

    def test_schema_covers_every_kind(self):
        schema = json.loads((ROOT / "contracts" / "events.schema.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(set(schema["properties"]["kind"]["enum"]), set(KINDS))
        self.assertEqual(set(schema["$defs"]["payload"]), set(KINDS))
        for kind, fields in KINDS.items():
            self.assertEqual(set(schema["$defs"]["payload"][kind]["required"]),
                             set(fields))


class FlowFixtureTest(unittest.TestCase):
    """fixtures/session_flow.json 必须能被内核从头还原且全部规则通过。"""

    def test_sample_flow_replays(self):
        from src.projection import Kernel

        events = json.loads((ROOT / "fixtures" / "session_flow.json")
                            .read_text(encoding="utf-8"))
        k = Kernel()
        k.apply_many(events)
        st = k.sessions["s-0923-pm"]
        self.assertTrue(st.frozen, "样例流终态应为已冻结")
        self.assertTrue(any(t.checked_in for t in st.teams.values()))
        # 第二遍重放必须幂等
        k2 = Kernel()
        k2.apply_many(events)
        self.assertEqual(
            [sorted(t.checked_in) for s in (st,) for t in s.teams.values()],
            [sorted(t.checked_in) for t in k2.sessions["s-0923-pm"].teams.values()],
        )


if __name__ == "__main__":
    unittest.main()
