"""测试公共构造：一个标准门店、空间版本、活动与可报名场次。"""

from src.app import Application


def ts(h, m=0, day=23):
    return f"2026-09-{day:02d}T{h:02d}:{m:02d}:00+08:00"


ZONES = [
    {"zone_id": "atrium", "name": "中庭", "floor": 1,
     "participant_cap": 6, "spectator_cap": 8, "fire_exit": True},
    {"zone_id": "east", "name": "东侧通道", "floor": 1,
     "participant_cap": 4, "spectator_cap": 12, "fire_exit": False},
]


def adult(age=25, signed=True):
    return {"age": age, "guardian_present": True, "safety_notice_signed": signed}


def minor(age=12, guardian=True, signed=True):
    return {"age": age, "guardian_present": guardian, "safety_notice_signed": signed}


def build_app(*, zones=None, participant_cap=4, age_min=10,
              risk_level="LOW", safety_requirements=()):
    app = Application()
    app.draft_space("S1", "v1", "中庭布局", zones or ZONES, ts(8), ts(8))
    app.publish_space("S1", "v1", ts(8, 5))
    for z in (zones or ZONES):
        app.activate_zone("S1", z["zone_id"], "v1", ts(8, 10))
    app.register_activity(
        activity_id="dou", name="拼豆", gameplay="限时拼豆",
        risk_level=risk_level, team_size_min=2, team_size_max=3,
        age_min=age_min, safety_requirements=safety_requirements,
        prizes=[{"rank": 1, "name": "代金券"}, {"rank": 2, "name": "小礼品"}],
        now=ts(8, 15),
    )
    app.schedule_session("S1", "M1", {
        "activity_id": "dou", "activity_name": "拼豆", "risk_level": risk_level,
        "age_min": age_min, "space_version": "v1", "zone_id": "atrium",
        "start_at": ts(10), "cutoff_at": ts(10, 15), "end_at": ts(11),
        "participant_cap": participant_cap, "spectator_cap": 8,
        "spectators_per_team_max": 2, "team_size_min": 2, "team_size_max": 3,
        "heat_size": 2,
    }, ts(9))
    app.freeze_session(
        "S1", "M1", ts(9, 10), venue_confirmed=True,
        staff=["st1"], referees=["ref1"], emergency_plan_id="EP-1",
        guardian_station=True,
    )
    app.open_signup("S1", "M1", ts(9, 15))
    return app


def register(app, rid, now, *, profiles=None, spectators=1, command_id=None,
             session_id="M1", contact="cust"):
    return app.register_team(
        "S1", session_id, now,
        registration_id=rid, team_name=f"队{rid}",
        member_profiles=profiles or [adult(), adult()],
        spectator_count=spectators, contact=contact,
        command_id=command_id or f"cmd-{rid}",
    )
