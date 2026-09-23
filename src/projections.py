"""读模型：全部由事件重放得到，不接受任何外部写入。

视图只陈述事实：排队长度、候补位次、分区暂停与安排、真实占用。
任何视图都不包含消费金额字段，资格视图只呈现与安全/规则相关的状态。
"""

from collections import defaultdict

from .contract import parse_ts
from .domain import Consent


class Projection:
    def handle(self, event):
        handler = getattr(self, f"on_{event['kind'].lower()}", None)
        if handler:
            handler(event["data"], event)


class OccupancyView(Projection):
    """分区实时占用：参赛者与围观者分别计数。

    计数依据票事件：入场票 +，票释放 -。票由聚合保证幂等，
    因此重复扫码 / 断网补签不会被重复计数。
    """

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self._ticket_sizes: dict[str, tuple[str, int, int]] = {}
        self.occupancy: dict[tuple[str, str], dict] = defaultdict(
            lambda: {"participants": 0, "spectators": 0}
        )

    def on_session_scheduled(self, d, e):
        self.sessions[e["subject_id"]] = {**d, "stream_id": e["subject_id"]}

    def on_admission_ticketed(self, d, _):
        s = self.sessions[d["session_id"]]
        key = (s["store_id"], s["zone_id"])
        self.occupancy[key]["participants"] += d["participants"]
        self.occupancy[key]["spectators"] += d["spectators"]
        self._ticket_sizes[d["ticket_id"]] = (d["session_id"], d["participants"], d["spectators"])

    def on_admission_released(self, d, _):
        entry = self._ticket_sizes.pop(d["ticket_id"], None)
        if entry is None:
            return
        session_id, participants, spectators = entry
        s = self.sessions[session_id]
        key = (s["store_id"], s["zone_id"])
        self.occupancy[key]["participants"] -= participants
        self.occupancy[key]["spectators"] -= spectators

    def for_zone(self, store_id, zone_id):
        return dict(self.occupancy.get((store_id, zone_id), {"participants": 0, "spectators": 0}))

    def load_ratio(self, store_id, zone, observed_spectators=0):
        """把现场观测与容量比较，返回参赛者/围观者两个饱和度。"""
        occ = self.for_zone(store_id, zone["zone_id"])
        return {
            "zone_id": zone["zone_id"],
            "participant_ratio": round(occ["participants"] / max(zone["participant_cap"], 1), 3),
            "spectator_ratio": round(
                (occ["spectators"] + observed_spectators) / max(zone["spectator_cap"], 1), 3
            ),
        }


class CustomerBoardView(Projection):
    """顾客随时可见的真实排队与变更视图。"""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.notices: dict[str, list[dict]] = defaultdict(list)
        self.pauses: dict[str, dict] = {}  # zone_id -> 当前暂停信息

    def on_session_scheduled(self, d, e):
        self.sessions[e["subject_id"]] = {
            "session_id": e["subject_id"],
            "activity_name": d["activity_name"],
            "zone_id": d["zone_id"],
            "start_at": d["start_at"],
            "status": "SCHEDULED",
        }

    def on_signup_opened(self, d, e):
        self.sessions[e["subject_id"]]["status"] = "SIGNUP_OPEN"

    def on_session_change_published(self, d, e):
        self.notices[e["subject_id"]].append(d)

    def on_zone_paused(self, d, e):
        # subject: zoneops:store:zone —— 末段即 zone_id
        zone_id = e["subject_id"].rsplit(":", 1)[-1]
        self.pauses[zone_id] = d

    def on_zone_resumed(self, d, e):
        zone_id = e["subject_id"].rsplit(":", 1)[-1]
        self.pauses.pop(zone_id, None)

    def board(self, session_views):
        """session_views: {session_id: 排队信息（由应用层从聚合投影给出）}。"""
        rows = []
        for sid, s in self.sessions.items():
            q = session_views.get(sid, {})
            pause = self.pauses.get(s["zone_id"])
            rows.append({
                "session_id": sid,
                "activity_name": s["activity_name"],
                "start_at": s["start_at"],
                "status": "PAUSED" if pause else s["status"],
                "accepting": q.get("accepting", True),
                "open_slots": q.get("open_slots"),
                "waitlist_len": q.get("waitlist_len", 0),
                "checked_in": q.get("checked_in", 0),
                "latest_notice": self.notices[sid][-1] if self.notices[sid] else None,
                "onsite_arrangement": pause["arrangement"] if pause else None,
            })
        return rows


class ConsentDirectory(Projection):
    """按人汇总授权事件，供素材发布前判定。"""

    def __init__(self):
        self._events: dict[str, list[dict]] = defaultdict(list)

    def handle(self, event):
        if event["kind"] in ("CONSENT_GRANTED", "CONSENT_WITHDRAWN"):
            person_id = event["subject_id"].split(":", 1)[1]
            self._events[person_id].append(event)

    def for_person(self, person_id) -> Consent:
        return Consent.load(f"consent:{person_id}", self._events.get(person_id, []))

    def licensed(self, person_id, scope, at):
        return self.for_person(person_id).licensed_for(scope, at)


class ExperienceMetrics(Projection):
    """区域团队比较体验与停留；只有评分和停留，没有消费。"""

    def __init__(self):
        self.by_session: dict[str, list[dict]] = defaultdict(list)

    def on_experience_sampled(self, d, _):
        session_id = d.get("session_id") or "UNKNOWN"
        self.by_session[session_id].append(d)

    def summary(self, session_id):
        samples = self.by_session.get(session_id, [])
        if not samples:
            return None
        ratings = [s["rating"] for s in samples]
        dwell = [s["dwell_minutes"] for s in samples]
        return {
            "samples": len(samples),
            "avg_rating": round(sum(ratings) / len(ratings), 2),
            "avg_dwell_minutes": round(sum(dwell) / len(dwell), 1),
        }

    def compare(self, session_ids):
        out = {}
        for sid in session_ids:
            summary = self.summary(sid)
            if summary:
                out[sid] = summary
        return out


def replay(store, *views):
    """把存储中的全部事件依次喂给各视图。"""
    for event in store.read_all():
        for view in views:
            view.handle(event)
    return views
