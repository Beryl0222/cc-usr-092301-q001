"""事件投影：把事件流还原为当前状态，并在写入时强制执行不变量。

严格语义：``Kernel.apply(event)`` 只做一次决定——
* 事件幂等（event_id、扫码 client_record_id），重放不产生副作用；
* 违反不变量的事件直接拒绝（``Rejected``），系统宁可拒绝也不破坏规则；
* 所有时间比较使用事件负载中的业务时间（如 scanned_at / observed_at），
  因此断网后补签的扫码按*实际发生时刻*判定迟到与暂停，而不是按同步时刻。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .events import KINDS, POST_FREEZE_ALLOWED, validate_event

READINESS_AREAS = ("VENUE", "STAFFING", "EMERGENCY_PLAN")
AUDIENCES = ("COMPETITOR", "SPECTATOR")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Rejected(Exception):
    """事件违反不变量，拒绝写入。code 为机器可读原因。"""

    def __init__(self, code: str, event_id: str | None = None):
        super().__init__(f"{code} ({event_id})" if event_id else code)
        self.code = code


@dataclass
class Member:
    member_id: str
    is_minor: bool
    guardian_id: str | None = None


@dataclass
class ZoneDef:
    zone_id: str
    name: str
    role: str
    competitor_capacity: int
    spectator_capacity: int
    adjacent_zone_ids: tuple[str, ...] = ()


@dataclass
class TeamState:
    team_id: str
    members: dict[str, Member]
    status: str = "REGISTERED"          # REGISTERED/ACCEPTED/WAITLIST/CHECKED_IN/LATE/CANCELLED/FORFEITED
    waitlist_position: int | None = None
    admit_from: datetime | None = None
    admit_until: datetime | None = None
    checked_in: set[str] = field(default_factory=set)
    first_checkin_at: datetime | None = None
    cancelled_at: datetime | None = None
    had_checked_in: bool = False
    releasable: bool = False            # 取消是否归还名额（迟到取消/已到场 → False）
    forfeited: bool = False


@dataclass
class Suspension:
    suspension_id: str
    zone_ids: frozenset[str]
    reason: str
    started_at: datetime
    ended_at: datetime | None = None
    present_teams: frozenset[str] = frozenset()  # 暂停开始时已到场队伍快照
    arrangements: list[dict] = field(default_factory=list)


@dataclass
class Incident:
    incident_id: str
    zone_id: str
    kind: str
    severity: str
    status: str = "OPEN"
    responders: dict[str, str] = field(default_factory=dict)  # responder_id -> role


@dataclass
class Consent:
    consent_id: str
    member_id: str
    scopes: frozenset[str]
    granted_by: str
    granted_at: datetime
    withdrawn_at: datetime | None = None


@dataclass
class Asset:
    asset_id: str
    incident_id: str | None
    sensitive: bool
    consent_ids: tuple[str, ...]
    captured_at: datetime
    access: dict[str, tuple[str, datetime, datetime | None]] = field(default_factory=dict)
    # responder_id -> (purpose, granted_at, expires_at)


@dataclass
class SessionState:
    session_id: str
    game_id: str | None = None
    venue_version_id: str | None = None
    zone_ids: tuple[str, ...] = ()
    slot_start: datetime | None = None
    slot_end: datetime | None = None
    competitor_capacity: int = 0
    spectator_capacity: int = 0
    roster_deadline: datetime | None = None
    late_grace: timedelta = timedelta(0)
    eligibility_rules: tuple[dict, ...] = ()
    teams: dict[str, TeamState] = field(default_factory=dict)
    waitlist: list[str] = field(default_factory=list)
    seats_used: int = 0                  # 已占参赛名额（人数），迟到取消不回收
    readiness: dict[str, dict[str, str]] = field(default_factory=dict)
    readiness_started: datetime | None = None
    frozen: bool = False
    matches: dict[str, dict] = field(default_factory=dict)
    prizes: dict[str, dict] = field(default_factory=dict)
    queue_notices: dict[str, dict] = field(default_factory=dict)
    changes: list[dict] = field(default_factory=list)
    occupancy: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    latest_obs: dict[str, dict] = field(default_factory=dict)
    suspensions: dict[str, Suspension] = field(default_factory=dict)
    incidents: dict[str, Incident] = field(default_factory=dict)
    groups: list[dict] = field(default_factory=list)
    dwell: dict[str, list[timedelta]] = field(default_factory=dict)
    merchant_tasks: dict[str, dict] = field(default_factory=dict)
    threshold_events: list[dict] = field(default_factory=list)
    frozen_at: datetime | None = None
    resolution: str | None = None
    _inside_since: dict[str, datetime] = field(default_factory=dict)


class Kernel:
    """内存内核：登记全局版本 + 各场次投影，同时充当不变量守门人。"""

    def __init__(self) -> None:
        self.event_ids: set[str] = set()
        self.versions: dict[str, int] = {}
        self.venues: dict[str, dict] = {}
        self.games: dict[str, dict] = {}
        self.sessions: dict[str, SessionState] = {}
        # 扫码幂等的全局账本：无论在线还是断网补签，同一 client_record_id 只生效一次
        self.scan_records: dict[str, str] = {}
        self.batches: set[str] = set()
        # 素材：授权与资产跨事件登记在核上（授权可以先于场次材料存在）
        self.consents: dict[str, Consent] = {}
        self.assets: dict[str, Asset] = {}
        self.release_requests: dict[str, dict] = {}

    # ---- 对外入口 ----------------------------------------------------------

    def apply(self, event: dict) -> SessionState | dict | None:
        errors = validate_event(event)
        if errors:
            raise Rejected(errors[0], event.get("event_id"))
        kind = event["kind"]
        eid = event["event_id"]
        if eid in self.event_ids:
            return None  # 标准幂等重放：静默跳过
        subject = event["subject_id"]
        expected = self.versions.get(subject, 0) + 1
        if event["version"] != expected:
            raise Rejected(f"BAD_VERSION:expected{expected}", eid)

        handler = getattr(self, f"_on_{kind.lower()}", None)
        if handler is None:
            raise Rejected(f"NO_HANDLER:{kind}", eid)
        result = handler(event["payload"], event)
        self.event_ids.add(eid)
        self.versions[subject] = event["version"]
        return result

    def apply_many(self, events: list[dict]) -> int:
        for ev in events:
            self.apply(ev)
        return len(events)

    # ---- 内部工具 ----------------------------------------------------------

    def _session_by_subject(self, ev: dict) -> SessionState:
        st = self.sessions.get(ev["subject_id"])
        if st is None:
            raise Rejected("SESSION_UNKNOWN", ev["event_id"])
        if st.frozen and ev["kind"] not in POST_FREEZE_ALLOWED:
            raise Rejected("FROZEN_STRUCTURAL_CHANGE", ev["event_id"])
        return st

    def _session(self, payload: dict, event: dict) -> SessionState:
        sid = payload["session_id"]
        if event["subject_id"] != sid:
            raise Rejected("SUBJECT_MISMATCH", event["event_id"])
        return self._session_by_subject(event)

    def _zone(self, st: SessionState, zone_id: str) -> ZoneDef:
        venue = self.venues[st.venue_version_id]
        if zone_id not in st.zone_ids:
            raise Rejected("ZONE_NOT_IN_SESSION")
        return venue["zones"][zone_id]

    def _suspension_active(self, st: SessionState, zone_id: str, at: datetime) -> Suspension | None:
        for sp in st.suspensions.values():
            if zone_id in sp.zone_ids and sp.started_at <= at and (
                sp.ended_at is None or at < sp.ended_at
            ):
                return sp
        return None

    def _present_teams(self, st: SessionState, zones: frozenset[str], at: datetime) -> frozenset[str]:
        """暂停时刻已经签到入场的队伍（安排的唯一合法接收人）。"""
        found: set[str] = set()
        for team in st.teams.values():
            if team.checked_in and team.first_checkin_at and team.first_checkin_at <= at:
                found.add(team.team_id)
        return frozenset(found)

    # ---- 空间与玩法 --------------------------------------------------------

    def _on_venue_version_published(self, p: dict, ev: dict) -> dict:
        if ev["subject_id"] != p["venue_version_id"]:
            raise Rejected("SUBJECT_MISMATCH", ev["event_id"])
        if p["venue_version_id"] in self.venues:
            raise Rejected("VENUE_VERSION_IMMUTABLE", ev["event_id"])
        zones = {}
        for z in p["zones"]:
            zones[z["zone_id"]] = ZoneDef(
                zone_id=z["zone_id"], name=z["name"], role=z["role"],
                competitor_capacity=z["competitor_capacity"],
                spectator_capacity=z["spectator_capacity"],
                adjacent_zone_ids=tuple(z.get("adjacent_zone_ids", [])),
            )
        venue = {"store_id": p["store_id"], "floor": p["floor"], "zones": zones}
        self.venues[p["venue_version_id"]] = venue
        return venue

    def _on_game_registered(self, p: dict, ev: dict) -> dict:
        if ev["subject_id"] != p["game_id"]:
            raise Rejected("SUBJECT_MISMATCH", ev["event_id"])
        if p["risk_level"] not in ("LOW", "MEDIUM", "HIGH"):
            raise Rejected("BAD_RISK_LEVEL", ev["event_id"])
        if p["team_size_min"] > p["team_size_max"] or p["age_min"] > p["age_max"]:
            raise Rejected("BAD_GAME_RANGE", ev["event_id"])
        game = dict(p)
        self.games[p["game_id"]] = game
        return game

    def _on_game_scheduled(self, p: dict, ev: dict) -> SessionState:
        sid = p["session_id"]
        if ev["subject_id"] != sid:
            raise Rejected("SUBJECT_MISMATCH", ev["event_id"])
        if sid in self.sessions:
            raise Rejected("SESSION_EXISTS", ev["event_id"])
        if p["game_id"] not in self.games:
            raise Rejected("GAME_UNKNOWN", ev["event_id"])
        venue = self.venues.get(p["venue_version_id"])
        if venue is None:
            raise Rejected("VENUE_UNKNOWN", ev["event_id"])
        for z in p["zone_ids"]:
            if z not in venue["zones"]:
                raise Rejected("ZONE_UNKNOWN", ev["event_id"])
        st = SessionState(session_id=sid)
        st.game_id = p["game_id"]
        st.venue_version_id = p["venue_version_id"]
        st.zone_ids = tuple(p["zone_ids"])
        st.slot_start = parse_ts(p["slot_start"])
        st.slot_end = parse_ts(p["slot_end"])
        st.competitor_capacity = p["competitor_capacity"]
        st.spectator_capacity = p["spectator_capacity"]
        st.roster_deadline = parse_ts(p["roster_deadline"])
        st.late_grace = timedelta(minutes=p["late_grace_minutes"])
        for z in st.zone_ids:
            st.occupancy[z] = {"COMPETITOR": set(), "SPECTATOR": set()}
        self.sessions[sid] = st
        return st

    # ---- 资格与报名 --------------------------------------------------------

    def _on_eligibility_rule_set(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        allowed = {"AGE_MIN", "AGE_MAX", "GUARDIAN_FOR_MINORS", "HEALTH_DECLARATION"}
        for rule in p["rules"]:
            if rule.get("type") not in allowed:
                # 目录里不存在、也永远不接受消费类门槛
                raise Rejected(f"ELIGIBILITY_RULE_UNKNOWN:{rule.get('type')}", ev["event_id"])
        st.eligibility_rules = tuple(p["rules"])
        return st

    def _on_team_registered(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        if p["team_id"] in st.teams:
            raise Rejected("TEAM_EXISTS", ev["event_id"])
        game = self.games[st.game_id]
        members = {}
        for m in p["members"]:
            guardian = m.get("guardian_id")
            if m["is_minor"] and not guardian:
                raise Rejected("MINOR_NEEDS_GUARDIAN", ev["event_id"])
            members[m["member_id"]] = Member(
                m["member_id"], m["is_minor"], guardian,
            )
        if not (game["team_size_min"] <= len(members) <= game["team_size_max"]):
            raise Rejected("TEAM_SIZE_OUT_OF_RANGE", ev["event_id"])
        st.teams[p["team_id"]] = TeamState(team_id=p["team_id"], members=members)
        return st

    def _on_registration_accepted(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        team = st.teams.get(p["team_id"])
        if team is None or team.status != "REGISTERED":
            raise Rejected("TEAM_NOT_PENDING", ev["event_id"])
        if st.seats_used + len(team.members) > st.competitor_capacity:
            raise Rejected("COMPETITOR_CAPACITY_FULL", ev["event_id"])
        team.status = "ACCEPTED"
        st.seats_used += len(team.members)
        return st

    def _on_registration_rejected(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        team = st.teams.get(p["team_id"])
        if team is None or team.status != "REGISTERED":
            raise Rejected("TEAM_NOT_PENDING", ev["event_id"])
        team.status = "REJECTED"
        return st

    def _on_waitlist_entered(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        team = st.teams.get(p["team_id"])
        if team is None or team.status != "REGISTERED":
            raise Rejected("TEAM_NOT_PENDING", ev["event_id"])
        if p["position"] != len(st.waitlist) + 1:
            raise Rejected("BAD_WAITLIST_POSITION", ev["event_id"])
        team.status = "WAITLIST"
        team.waitlist_position = p["position"]
        st.waitlist.append(team.team_id)
        return st

    def _on_waitlist_promoted(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        team = st.teams.get(p["team_id"])
        if team is None or team.status != "WAITLIST":
            raise Rejected("TEAM_NOT_WAITLISTED", ev["event_id"])
        if st.seats_used + len(team.members) > st.competitor_capacity:
            # 迟到取消/已到场取消不释放名额，候补无法顶上空缺
            raise Rejected("NO_RELEASABLE_SEAT", ev["event_id"])
        if st.waitlist and st.waitlist[0] != team.team_id:
            raise Rejected("WAITLIST_ORDER", ev["event_id"])
        st.waitlist.pop(0)
        for t in st.teams.values():
            if t.status == "WAITLIST" and t.waitlist_position:
                t.waitlist_position -= 1
        team.status = "ACCEPTED"
        team.waitlist_position = None
        st.seats_used += len(team.members)
        return st

    # ---- 分时入场 ----------------------------------------------------------

    def _assign_window(self, team: TeamState, p: dict) -> None:
        team.admit_from = parse_ts(p["admit_from"])
        team.admit_until = parse_ts(p["admit_until"])
        if team.admit_from >= team.admit_until:
            raise Rejected("BAD_ENTRY_WINDOW")

    def _on_entry_slot_assigned(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        team = st.teams.get(p["team_id"])
        if team is None or team.status not in ("ACCEPTED",):
            raise Rejected("TEAM_NOT_ACCEPTED", ev["event_id"])
        self._assign_window(team, p)
        return st

    def _on_entry_slot_rescheduled(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        team = st.teams.get(p["team_id"])
        if team is None:
            raise Rejected("TEAM_UNKNOWN", ev["event_id"])
        self._assign_window(team, p)
        st.changes.append({
            "type": "ENTRY_SLOT_RESCHEDULED", "team_id": team.team_id,
            "admit_from": p["admit_from"], "admit_until": p["admit_until"],
            "reason": p["reason"], "visible_at": p["admit_from"],
        })
        return st

    # ---- 扫码签到（重复扫码 / 断网补签） -----------------------------------

    def _admit_scan(self, st: SessionState, team: TeamState, zone_id: str, at: datetime) -> str:
        """返回扫码裁决：OK / LATE / ZONE_SUSPENDED。"""
        if self._suspension_active(st, zone_id, at) is not None:
            return "ZONE_SUSPENDED"
        if team.admit_until is not None:
            deadline = team.admit_until + st.late_grace
            if at > deadline:
                return "LATE"
        return "OK"

    def _do_scan(self, st: SessionState, p: dict, ev: dict) -> str:
        crid = p["client_record_id"]
        if crid in self.scan_records:
            # 同一台终端的重复扫码、或在线已扫后离线批次再次上送：只认第一次
            return "DEDUP"
        team = st.teams.get(p["team_id"])
        if team is None:
            raise Rejected("TEAM_UNKNOWN", ev["event_id"])
        member = team.members.get(p["member_id"])
        if member is None:
            raise Rejected("MEMBER_UNKNOWN", ev["event_id"])
        if team.status not in ("ACCEPTED", "CHECKED_IN"):
            raise Rejected("TEAM_NOT_ACCEPTED", ev["event_id"])
        self._zone(st, p["zone_id"])
        at = parse_ts(p["scanned_at"])
        verdict = self._admit_scan(st, team, p["zone_id"], at)
        if verdict in ("LATE", "ZONE_SUSPENDED"):
            # 裁决类拒绝不写入 scan_records：网络恢复/暂停解除后顾客还能重试
            return verdict
        self.scan_records[crid] = ev["event_id"]
        if member.member_id not in team.checked_in:
            team.checked_in.add(member.member_id)
            if team.first_checkin_at is None or at < team.first_checkin_at:
                team.first_checkin_at = at
        team.status = "CHECKED_IN"
        return "OK"

    def _on_checkin_scanned(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        if not isinstance(p["offline"], bool):
            raise Rejected("BAD_OFFLINE_FLAG", ev["event_id"])
        self._do_scan(st, p, ev)
        return st

    def _on_checkin_dedup_rejected(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        if p["client_record_id"] not in self.scan_records:
            raise Rejected("DEDUP_WITHOUT_ORIGINAL", ev["event_id"])
        return st

    def _on_offline_batch_synced(self, p: dict, ev: dict) -> SessionState:
        st = self.sessions.get(ev["subject_id"])
        if st is None:
            raise Rejected("SESSION_UNKNOWN", ev["event_id"])
        if p["batch_id"] in self.batches:
            raise Rejected("BATCH_DUPLICATE", ev["event_id"])
        self.batches.add(p["batch_id"])
        for rec in p["records"]:
            # 用扫描时的业务时间合成事件，逐条走同一套守门逻辑
            synth = {
                "event_id": f"{p['batch_id']}:{rec['client_record_id']}",
                "kind": "CHECKIN_SCANNED",
                "occurred_at": rec["scanned_at"],
                "subject_id": st.session_id,
                "version": 0,
                "payload": {
                    "scan_id": rec.get("scan_id", rec["client_record_id"]),
                    "session_id": st.session_id,
                    "zone_id": rec["zone_id"],
                    "team_id": rec["team_id"],
                    "member_id": rec["member_id"],
                    "scanned_at": rec["scanned_at"],
                    "scanner_id": p["scanner_id"],
                    "offline": True,
                    "client_record_id": rec["client_record_id"],
                },
            }
            verdict = self._do_scan(st, synth["payload"], synth)
            if verdict == "DEDUP":
                continue  # 在线已扫过，补签不产生任何效果
            if verdict in ("LATE", "ZONE_SUSPENDED"):
                # 补签同样受迟到与暂停约束，不会把不合规签到补成到场
                continue
        return st

    # ---- 迟到 / 取消 / 弃权 -----------------------------------------------

    def _on_late_arrival_marked(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        team = st.teams.get(p["team_id"])
        if team is None:
            raise Rejected("TEAM_UNKNOWN", ev["event_id"])
        if team.checked_in or team.status in ("CHECKED_IN", "CANCELLED", "FORFEITED"):
            raise Rejected("TEAM_ALREADY_PRESENT_OR_CLOSED", ev["event_id"])
        team.status = "LATE"
        return st

    def _on_team_cancelled(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        team = st.teams.get(p["team_id"])
        if team is None or team.status in ("CANCELLED", "FORFEITED"):
            raise Rejected("TEAM_NOT_ACTIVE", ev["event_id"])
        at = parse_ts(p["cancelled_at"])
        if p["had_checked_in"] != bool(team.checked_in):
            raise Rejected("CHECKIN_FLAG_MISMATCH", ev["event_id"])
        was_waitlisted = team.status == "WAITLIST"
        if was_waitlisted or team.waitlist_position is not None:
            st.waitlist = [t for t in st.waitlist if t != team.team_id]
            for t in st.teams.values():
                if t.status == "WAITLIST" and (t.waitlist_position or 0) > (
                    team.waitlist_position or 0
                ):
                    t.waitlist_position = (t.waitlist_position or 1) - 1
        team.status = "CANCELLED"
        team.cancelled_at = at
        team.had_checked_in = p["had_checked_in"]
        # 只有"未到场且在名单截止前"的取消才归还名额；
        # 已到场或迟到的取消绝不制造额外名额。
        team.releasable = (
            not team.had_checked_in
            and not was_waitlisted
            and st.roster_deadline is not None and at <= st.roster_deadline
        )
        if team.releasable:
            st.seats_used -= len(team.members)
        return st

    def _on_slot_forfeited(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        team = st.teams.get(p["team_id"])
        if team is None or team.status in ("CANCELLED", "FORFEITED"):
            raise Rejected("TEAM_NOT_ACTIVE", ev["event_id"])
        team.status = "FORFEITED"
        team.forfeited = True
        team.releasable = False  # 弃权/不到：名额作废，不顶补
        return st

    # ---- 分组与裁判 --------------------------------------------------------

    def _on_bracket_built(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        assigned: set[str] = set()
        for g in p["groups"]:
            self._zone(st, g["zone_id"])
            for tid in g["team_ids"]:
                team = st.teams.get(tid)
                if team is None or team.status not in ("ACCEPTED", "CHECKED_IN"):
                    raise Rejected("BRACKET_TEAM_INELIGIBLE", ev["event_id"])
                if tid in assigned:
                    raise Rejected("BRACKET_TEAM_DUPLICATED", ev["event_id"])
                assigned.add(tid)
        st.groups = list(p["groups"])
        return st

    def _on_heat_called(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        if not any(g["group_id"] == p["group_id"] for g in st.groups):
            raise Rejected("GROUP_UNKNOWN", ev["event_id"])
        return st

    def _on_match_result_recorded(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        if not any(g["group_id"] == p["group_id"] for g in st.groups):
            raise Rejected("GROUP_UNKNOWN", ev["event_id"])
        st.matches[p["match_id"]] = {
            "group_id": p["group_id"], "referee_id": p["referee_id"],
            "rankings": list(p["rankings"]),
            "recorded_at": p["recorded_at"], "amendments": [],
        }
        return st

    def _on_result_amended(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        match = st.matches.get(p["match_id"])
        if match is None:
            raise Rejected("MATCH_UNKNOWN", ev["event_id"])
        entry = {
            "amendment_id": p["amendment_id"], "referee_id": p["referee_id"],
            "rankings": list(p["rankings"]), "reason": p["reason"],
            "amended_at": p["amended_at"],
        }
        if any(a["amendment_id"] == p["amendment_id"] for a in match["amendments"]):
            raise Rejected("AMENDMENT_DUPLICATE", ev["event_id"])
        match["amendments"].append(entry)
        match["rankings"] = list(p["rankings"])  # 当前值可覆盖，历史全部保留
        match["referee_id"] = p["referee_id"]
        return st

    # ---- 奖品 --------------------------------------------------------------

    def _on_prize_defined(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        if p["basis"] not in ("PARTICIPATION", "RANK"):
            raise Rejected("BAD_PRIZE_BASIS", ev["event_id"])
        st.prizes[p["prize_id"]] = {"basis": p["basis"], "description": p["description"]}
        return st

    def _on_prize_awarded(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        prize = st.prizes.get(p["prize_id"])
        team = st.teams.get(p["team_id"])
        if prize is None or team is None:
            raise Rejected("PRIZE_OR_TEAM_UNKNOWN", ev["event_id"])
        if prize["basis"] == "PARTICIPATION" and not team.checked_in:
            raise Rejected("PRIZE_REQUIRES_CHECKIN", ev["event_id"])
        if prize["basis"] == "RANK":
            ranked = {t for m in st.matches.values() for t in
                      (r["team_id"] for r in m["rankings"])}
            if team.team_id not in ranked:
                raise Rejected("PRIZE_REQUIRES_RANK", ev["event_id"])
        prize.setdefault("awarded", []).append(
            {"team_id": team.team_id, "awarded_by": p["awarded_by"],
             "awarded_at": p["awarded_at"]},
        )
        return st

    # ---- 商户协作与顾客可见信息 --------------------------------------------

    def _on_merchant_task_assigned(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        self._zone(st, p["zone_id"])
        st.merchant_tasks[p["task_id"]] = {
            "merchant_id": p["merchant_id"], "zone_id": p["zone_id"],
            "kind": p["kind"], "window_start": p["window_start"],
            "window_end": p["window_end"], "confirmed": False,
        }
        return st

    def _on_merchant_task_confirmed(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        task = st.merchant_tasks.get(p["task_id"])
        if task is None:
            raise Rejected("TASK_UNKNOWN", ev["event_id"])
        task["confirmed"] = True
        task["handled_by"] = p["handled_by"]
        return st

    def _on_queue_notice_published(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        self._zone(st, p["zone_id"])
        at = parse_ts(p["observed_at"])
        prev = st.queue_notices.get(p["zone_id"])
        if prev is not None and at < parse_ts(prev["observed_at"]):
            raise Rejected("STALE_NOTICE", ev["event_id"])
        st.queue_notices[p["zone_id"]] = dict(p)
        return st

    def _on_session_change_published(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        st.changes.append(dict(p))
        return st

    # ---- 客流观测 ----------------------------------------------------------

    def _on_crowd_observed(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        self._zone(st, p["zone_id"])
        st.latest_obs[p["zone_id"]] = dict(p)
        return st

    def _on_crowd_threshold_crossed(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        self._zone(st, p["zone_id"])
        if p["direction"] not in ("OVER", "UNDER"):
            raise Rejected("BAD_THRESHOLD_DIRECTION", ev["event_id"])
        st.threshold_events.append(dict(p))
        return st

    def _track_presence(self, p: dict, ev: dict, entering: bool) -> SessionState:
        st = self._session_by_subject(ev)
        zone = self._zone(st, p["zone_id"])
        if p["audience"] not in AUDIENCES:
            raise Rejected("BAD_AUDIENCE", ev["event_id"])
        at = parse_ts(p["observed_at"])
        key = (p["zone_id"], p["audience"], p["member_id"])
        occ = st.occupancy[p["zone_id"]][p["audience"]]
        cap = (zone.competitor_capacity if p["audience"] == "COMPETITOR"
               else zone.spectator_capacity)
        if entering:
            if key in st._inside_since:
                raise Rejected("DOUBLE_ENTRY", ev["event_id"])
            if len(occ) >= cap:
                raise Rejected(f"{p['audience']}_CAPACITY_FULL", ev["event_id"])
            occ.add(p["member_id"])
            st._inside_since[key] = at
        else:
            if key not in st._inside_since:
                raise Rejected("EXIT_WITHOUT_ENTRY", ev["event_id"])
            occ.discard(p["member_id"])
            started = st._inside_since.pop(key)
            st.dwell.setdefault(p["member_id"], []).append(at - started)
        return st

    def _on_zone_entry_observed(self, p: dict, ev: dict) -> SessionState:
        return self._track_presence(p, ev, True)

    def _on_zone_exit_observed(self, p: dict, ev: dict) -> SessionState:
        return self._track_presence(p, ev, False)

    # ---- 应急分区暂停 ------------------------------------------------------

    def _on_area_suspension_started(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        zids = frozenset(p["zone_ids"])
        for z in zids:
            self._zone(st, z)
        at = parse_ts(p["started_at"])
        present = self._present_teams(st, zids, at)
        sp = Suspension(
            suspension_id=p["suspension_id"], zone_ids=zids, reason=p["reason"],
            started_at=at, present_teams=present,
        )
        st.suspensions[p["suspension_id"]] = sp
        return st

    def _on_suspension_arrangement_issued(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        sp = st.suspensions.get(p["suspension_id"])
        if sp is None:
            raise Rejected("SUSPENSION_UNKNOWN", ev["event_id"])
        if sp.ended_at is not None:
            raise Rejected("SUSPENSION_LIFTED", ev["event_id"])
        team_ids = frozenset(p["team_ids"])
        if not team_ids <= sp.present_teams:
            # 安排只能发给暂停时确已到场者；未到场的走变更通知，不占用现场资源
            raise Rejected("ARRANGEMENT_FOR_NON_PRESENT", ev["event_id"])
        if p["action"] not in ("RELOCATE", "WAIT_ON_SITE", "DEFER_TO_SLOT"):
            raise Rejected("BAD_ARRANGEMENT_ACTION", ev["event_id"])
        if p["action"] == "RELOCATE":
            target = p.get("instruction", {}).get("target_zone_id")
            if not target or target in sp.zone_ids:
                raise Rejected("RELOCATE_TARGET_INVALID", ev["event_id"])
            self._zone(st, target)
            if self._suspension_active(st, target, parse_ts(p["issued_at"])) is not None:
                raise Rejected("RELOCATE_TARGET_SUSPENDED", ev["event_id"])
        sp.arrangements.append(dict(p))
        return st

    def _on_area_suspension_lifted(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        sp = st.suspensions.get(p["suspension_id"])
        if sp is None:
            raise Rejected("SUSPENSION_UNKNOWN", ev["event_id"])
        sp.ended_at = parse_ts(p["ended_at"])
        return st

    # ---- 安全事件与处置人 --------------------------------------------------

    def _on_incident_reported(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        self._zone(st, p["zone_id"])
        if p["kind"] not in ("INJURY", "MINOR_LOST", "OTHER"):
            raise Rejected("BAD_INCIDENT_KIND", ev["event_id"])
        st.incidents[p["incident_id"]] = Incident(
            incident_id=p["incident_id"], zone_id=p["zone_id"],
            kind=p["kind"], severity=p["severity"],
        )
        return st

    def _on_responder_assigned(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        inc = st.incidents.get(p["incident_id"])
        if inc is None:
            raise Rejected("INCIDENT_UNKNOWN", ev["event_id"])
        inc.responders[p["responder_id"]] = p["role"]
        return st

    def _on_minor_reunited(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        inc = st.incidents.get(p["incident_id"])
        if inc is None or inc.kind != "MINOR_LOST":
            raise Rejected("INCIDENT_NOT_MINOR_LOST", ev["event_id"])
        guardian_ok = False
        for team in st.teams.values():
            m = team.members.get(p["member_id"])
            if m and m.is_minor and m.guardian_id == p["guardian_id"]:
                guardian_ok = True
        if not guardian_ok:
            raise Rejected("GUARDIAN_MISMATCH", ev["event_id"])
        return st

    def _on_incident_status_changed(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        inc = st.incidents.get(p["incident_id"])
        if inc is None:
            raise Rejected("INCIDENT_UNKNOWN", ev["event_id"])
        inc.status = p["status"]
        return st

    def _on_incident_resolved(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        inc = st.incidents.get(p["incident_id"])
        if inc is None:
            raise Rejected("INCIDENT_UNKNOWN", ev["event_id"])
        inc.status = "RESOLVED"
        inc.resolution = p["resolution"]
        return st

    # ---- 素材授权 ----------------------------------------------------------

    def _on_media_consent_granted(self, p: dict, ev: dict) -> SessionState | None:
        sid = ev["subject_id"]
        st = self.sessions.get(sid)
        if st is not None:
            # 未成年人授权必须来自登记的监护人
            for team in st.teams.values():
                m = team.members.get(p["member_id"])
                if m is not None and m.is_minor and m.guardian_id != p["granted_by"]:
                    raise Rejected("CONSENT_NOT_FROM_GUARDIAN", ev["event_id"])
        if p["consent_id"] in self.consents:
            raise Rejected("CONSENT_EXISTS", ev["event_id"])
        self.consents[p["consent_id"]] = Consent(
            consent_id=p["consent_id"], member_id=p["member_id"],
            scopes=frozenset(p["scope"]), granted_by=p["granted_by"],
            granted_at=parse_ts(p["granted_at"]),
        )
        return st

    def _on_media_consent_withdrawn(self, p: dict, ev: dict) -> SessionState | None:
        consent = self.consents.get(p["consent_id"])
        if consent is None:
            raise Rejected("CONSENT_UNKNOWN", ev["event_id"])
        if consent.withdrawn_at is not None:
            raise Rejected("CONSENT_ALREADY_WITHDRAWN", ev["event_id"])
        consent.withdrawn_at = parse_ts(p["withdrawn_at"])
        return self.sessions.get(ev["subject_id"])

    def _on_media_asset_captured(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        self._zone(st, p["zone_id"])
        at = parse_ts(p["captured_at"])
        for cid in p["consent_ids"]:
            consent = self.consents.get(cid)
            if consent is None:
                raise Rejected("CONSENT_UNKNOWN", ev["event_id"])
            if consent.withdrawn_at is not None and consent.withdrawn_at <= at:
                raise Rejected("CAPTURE_AFTER_WITHDRAWAL", ev["event_id"])
        incident_id = p.get("incident_id")
        if p["sensitive"] and not incident_id:
            raise Rejected("SENSITIVE_NEEDS_INCIDENT", ev["event_id"])
        if incident_id and incident_id not in st.incidents:
            raise Rejected("INCIDENT_UNKNOWN", ev["event_id"])
        self.assets[p["asset_id"]] = Asset(
            asset_id=p["asset_id"], incident_id=incident_id,
            sensitive=bool(p["sensitive"]), consent_ids=tuple(p["consent_ids"]),
            captured_at=at,
        )
        return st

    def _on_asset_access_granted(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        asset = self.assets.get(p["asset_id"])
        if asset is None:
            raise Rejected("ASSET_UNKNOWN", ev["event_id"])
        inc = st.incidents.get(p["incident_id"])
        if inc is None or asset.incident_id != inc.incident_id:
            raise Rejected("ACCESS_WRONG_INCIDENT", ev["event_id"])
        if p["responder_id"] not in inc.responders:
            # 敏感材料只向实际处置人员开放
            raise Rejected("RESPONDER_NOT_ASSIGNED", ev["event_id"])
        granted_at, expires_at = parse_ts(p["granted_at"]), parse_ts(p["expires_at"])
        if expires_at <= granted_at:
            raise Rejected("BAD_ACCESS_EXPIRY", ev["event_id"])
        asset.access[p["responder_id"]] = (p["purpose"], granted_at, expires_at)
        return st

    def _on_asset_access_revoked(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        asset = self.assets.get(p["asset_id"])
        if asset is None or p["responder_id"] not in asset.access:
            raise Rejected("ACCESS_NOT_FOUND", ev["event_id"])
        asset.access[p["responder_id"]] = (
            asset.access[p["responder_id"]][0],
            asset.access[p["responder_id"]][1],
            parse_ts(p["revoked_at"]),  # 提前到期
        )
        return st

    def _on_media_release_requested(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        if p["asset_id"] not in self.assets:
            raise Rejected("ASSET_UNKNOWN", ev["event_id"])
        self.release_requests[p["request_id"]] = dict(p)
        return st

    def _on_media_release_decided(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        req = self.release_requests.get(p["request_id"])
        if req is None:
            raise Rejected("REQUEST_UNKNOWN", ev["event_id"])
        at = parse_ts(p["decided_at"])
        if p["decision"] == "APPROVE":
            asset = self.assets[req["asset_id"]]
            if asset.sensitive:
                raise Rejected("SENSITIVE_NOT_RELEASABLE", ev["event_id"])
            scope = "PUBLIC_PROMOTION" if req["channel"] != "ONSITE" else "ONSITE_DISPLAY"
            for cid in asset.consent_ids:
                consent = self.consents[cid]
                if consent.withdrawn_at is not None and consent.withdrawn_at <= at:
                    raise Rejected("CONSENT_WITHDRAWN", ev["event_id"])
                if scope not in consent.scopes:
                    raise Rejected("CONSENT_SCOPE_MISSING", ev["event_id"])
        req["decision"] = p["decision"]
        req["reason"] = p["reason"]
        return st

    # ---- 就绪与冻结 --------------------------------------------------------

    def _on_readiness_check_started(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        st.readiness_started = parse_ts(p["started_at"])
        return st

    def _on_readiness_item_confirmed(self, p: dict, ev: dict) -> SessionState:
        st = self._session(p, ev)
        if p["area"] not in READINESS_AREAS:
            raise Rejected("BAD_READINESS_AREA", ev["event_id"])
        st.readiness.setdefault(p["area"], {})[p["item"]] = p["confirmed_by"]
        return st

    def _on_plan_frozen(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        from .policy import readiness_gaps

        gaps = readiness_gaps(st, self)
        if gaps:
            raise Rejected(f"READINESS_INCOMPLETE:{','.join(gaps)}", ev["event_id"])
        st.frozen = True
        st.frozen_at = parse_ts(p["frozen_at"])
        return st

    def _on_plan_unfrozen(self, p: dict, ev: dict) -> SessionState:
        st = self._session_by_subject(ev)
        st.frozen = False
        return st
