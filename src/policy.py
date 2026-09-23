"""策略层：在投影之上给出可执行判定（不直接改变状态）。

这里的函数回答"现在能不能做"与"顾客/区域应该看到什么"，
所有决定都基于事件还原出的状态，因此可重放、可审计。

禁止项在此体现为"根本算不出来"：
* 资格函数没有消费金额入参，也没有任何基于消费的分支；
* 候补晋升只看 seats_used（迟到取消/弃权不回收名额的账已在投影层做掉）；
* 暂停影响范围严格按区域集合计算，其他区域照常运行。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .projection import SessionState, Kernel, parse_ts

READINESS_REQUIRED_ITEMS: dict[str, tuple[str, ...]] = {
    # area -> 至少需要确认的清单项（具体 item 名称即门店检查单的键）
    "VENUE": ("floor_marking", "egress_clear", "zone_capacity_plate"),
    "STAFFING": ("referee_assigned", "queue_steward_assigned",
                 "emergency_contact_known"),
    "EMERGENCY_PLAN": ("injury_flow_drilled", "minor_lost_flow_drilled",
                       "media_rule_briefed"),
}


def readiness_gaps(st: SessionState, kernel: Kernel) -> list[str]:
    """开场前：场地、人员、预案三块都齐备才能冻结。"""
    gaps: list[str] = []
    if st.readiness_started is None:
        gaps.append("READINESS_NOT_STARTED")
    for area, items in READINESS_REQUIRED_ITEMS.items():
        confirmed = st.readiness.get(area, {})
        for item in items:
            if item not in confirmed:
                gaps.append(f"{area}:{item}")
    if not any(kernel.venues[st.venue_version_id]["zones"][zid].role
               in ("playing", "egress") for zid in st.zone_ids):
        gaps.append("VENUE:no_playing_or_egress_zone")
    return gaps


# ---- 容量：参赛者与围观者两本独立账 ---------------------------------------

@dataclass
class CapacityView:
    zone_id: str
    competitor_capacity: int
    spectator_capacity: int
    competitors_inside: int
    spectators_inside: int
    suspended: bool

    @property
    def competitor_headroom(self) -> int:
        return max(0, self.competitor_capacity - self.competitors_inside)
    @property
    def spectator_headroom(self) -> int:
        return max(0, self.spectator_capacity - self.spectators_inside)


def zone_capacity_view(kernel: Kernel, st: SessionState, zone_id: str,
                       at: datetime | str) -> CapacityView:
    if isinstance(at, str):
        at = parse_ts(at)
    zone = kernel.venues[st.venue_version_id]["zones"][zone_id]
    occ = st.occupancy[zone_id]
    suspended = kernel._suspension_active(st, zone_id, at) is not None
    return CapacityView(
        zone_id=zone_id,
        competitor_capacity=zone.competitor_capacity,
        spectator_capacity=zone.spectator_capacity,
        competitors_inside=len(occ["COMPETITOR"]),
        spectators_inside=len(occ["SPECTATOR"]),
        suspended=suspended,
    )


def session_seats_view(st: SessionState) -> dict:
    """参赛名额账：迟到取消与弃权永不归还。"""
    active = [t for t in st.teams.values()
              if t.status in ("ACCEPTED", "CHECKED_IN", "LATE")]
    return {
        "competitor_capacity": st.competitor_capacity,
        "seats_held": st.seats_used,
        "headroom": max(0, st.competitor_capacity - st.seats_used),
        "waitlist_len": len(st.waitlist),
        "active_teams": len(active),
    }


# ---- 暂停影响面：只影响被暂停区域，其他区域照常 ---------------------------

def affected_scope(kernel: Kernel, st: SessionState, suspension_id: str) -> dict:
    sp = st.suspensions[suspension_id]
    other_zones = [z for z in st.zone_ids if z not in sp.zone_ids]
    return {
        "suspended_zones": sorted(sp.zone_ids),
        "other_zones": other_zones,
        "present_teams": sorted(sp.present_teams),
        "arrangement_recipients": sorted(
            {t for a in sp.arrangements for t in a["team_ids"]}
        ),
        # 其他区域容量视图仍可正常入场（策略层可逐区计算 headroom）
        "other_zones_operational": True,
    }


# ---- 顾客可见视图：真实排队与变更 -----------------------------------------

def customer_queue_view(st: SessionState, zone_id: str) -> dict | None:
    notice = st.queue_notices.get(zone_id)
    if notice is None:
        return None
    return {
        "zone_id": zone_id,
        "expected_wait_minutes": notice["expected_wait_minutes"],
        "observed_at": notice["observed_at"],
    }


def customer_changes_view(st: SessionState, team_id: str | None = None) -> list[dict]:
    """公开变更流；队伍级改期只返回给当事队伍，其余人只见场次级公告。"""
    out = []
    for c in st.changes:
        if "team_id" in c and c["team_id"] != team_id:
            continue
        out.append({
            "change_type": c["type"] if "type" in c else c["change_type"],
            "message": c.get("message"),
            "at": c.get("visible_at"),
        })
    return out


# ---- 区域团队体验与停留效果对比（不含消费金额） ---------------------------

def experience_metrics(st: SessionState) -> dict:
    """区域团队用于比较玩法体验与停留效果；指标只有到场与时长，无消费字段。"""
    total_dwell_seconds = 0.0
    dwell_count = 0
    for spans in st.dwell.values():
        for d in spans:
            total_dwell_seconds += d.total_seconds()
            dwell_count += 1
    avg_dwell = (total_dwell_seconds / dwell_count) if dwell_count else 0.0
    arrivals = sum(1 for t in st.teams.values() if t.checked_in)
    no_shows = sum(1 for t in st.teams.values() if t.status == "FORFEITED")
    lates = sum(1 for t in st.teams.values() if t.status == "LATE")
    return {
        "checked_in_teams": arrivals,
        "no_show_teams": no_shows,
        "late_teams": lates,
        "avg_dwell_seconds": round(avg_dwell, 1),
        "sample_size": dwell_count,
    }
