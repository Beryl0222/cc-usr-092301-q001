"""由事件目录生成 JSON Schema（单一事实来源是 ``src.events.KINDS``）。"""
from __future__ import annotations

import json

from .events import KINDS, POST_FREEZE_ALLOWED

ENVELOPE = {
    "event_id": {"type": "string", "minLength": 1,
                 "description": "全局唯一、幂等；重放不产生副作用"},
    "kind": {"type": "string", "enum": sorted(KINDS)},
    "occurred_at": {"type": "string", "format": "date-time",
                    "description": "业务发生时刻（ISO-8601 带时区），断网补签也填实际时刻"},
    "subject_id": {"type": "string", "minLength": 1,
                   "description": "聚合主体，通常为场次 session_id"},
    "version": {"type": "integer", "minimum": 1,
                "description": "同一 subject_id 内单调递增"},
    "actor_id": {"type": "string"},
}


def build_schema() -> dict:
    payload_defs = {
        kind: {
            "type": "object",
            "required": list(fields),
            "properties": {f: {"description": "见 docs/domain.md"} for f in fields},
            "additionalProperties": True,
        }
        for kind, fields in KINDS.items()
    }
    payload_oneof = [
        {"properties": {"kind": {"const": kind}, "payload": {"$ref": f"#/$defs/payload/{kind}"}},
         "required": ["event_id", "kind", "occurred_at", "subject_id", "version", "payload"]}
        for kind in sorted(KINDS)
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "趣味赛事运行系统事件信封",
        "type": "object",
        "required": ["event_id", "kind", "occurred_at", "subject_id", "version"],
        "properties": ENVELOPE,
        "oneOf": payload_oneof,
        "$defs": {"payload": payload_defs,
                  "post_freeze_allowed": sorted(POST_FREEZE_ALLOWED)},
    }


def render() -> str:
    return json.dumps(build_schema(), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "contracts/events.schema.json"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render())
    print(f"wrote {out} ({len(KINDS)} kinds)")
