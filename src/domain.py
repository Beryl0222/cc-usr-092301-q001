"""领域聚合。

所有业务规则都在聚合的命令方法中判断，事件一旦产生即代表事实。
聚合不依赖时钟与存储，``now`` 由调用方传入，便于回放与测试。
"""

import random
from datetime import timedelta

from .contract import parse_ts


class DomainError(ValueError):
    """业务规则不满足。"""


class AccessDenied(PermissionError):
    """无权访问敏感材料。"""


RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")

# 资格判断只允许这些与消费无关的属性，从结构上杜绝消费门槛。
ALLOWED_PROFILE_KEYS = {"age", "guardian_present", "safety_notice_signed"}

INCIDENT_TYPES = ("INJURY", "MINOR_LOST", "DISPUTE", "OTHER")
PAUSE_TRIGGERS = (
    "FIRE_EXIT_BLOCKED",
    "ADJACENT_CONGESTION",
    "CROWD_DENSITY",
    "INCIDENT",
    "EQUIPMENT",
)


def _ts(now: str) -> "object":
    return parse_ts(now)


class Aggregate:
    def __init__(self, subject_id: str):
        self.id = subject_id
        self.version = 0
        self._commands: set[str] = set()

    @classmethod
    def load(cls, subject_id: str, events: list[dict]):
        agg = cls(subject_id)
        for event in events:
            if event["subject_id"] != subject_id:
                continue
            agg.when(event)
            agg.version = event["version"]
            cid = event.get("command_id")
            if cid:
                agg._commands.add(cid)
        return agg

    def _emit(self, kind, data, now, *, command_id=None, actor_id=None):
        # 命令幂等：同一 command_id 的重放不产生任何新事件。
        if command_id is not None and command_id in self._commands:
            return None
        event = {
            "event_id": f"{self.id}:{self.version + 1}",
            "kind": kind,
            "occurred_at": now,
            "subject_id": self.id,
            "version": self.version + 1,
            "data": data,
        }
        if command_id is not None:
            event["command_id"] = command_id
            self._commands.add(command_id)
        if actor_id is not None:
            event["actor_id"] = actor_id
        self.when(event)
        self.version += 1
        return event

    def when(self, event):
        raise NotImplementedError


# ---------------------------------------------------------------- 门店空间版本


class SpaceLayout(Aggregate):
    """门店空间版本：分区布局、参赛者/围观者容量、消防通道标记。"""

    def __init__(self, subject_id):
        super().__init__(subject_id)
        self.store_id = subject_id.split(":", 1)[1] if ":" in subject_id else subject_id
        self.versions: dict[str, dict] = {}

    def draft_version(self, space_version, label, zones, effective_from, now):
        if space_version in self.versions:
            raise DomainError(f"空间版本 {space_version} 已存在")
        if not zones:
            raise DomainError("至少需要定义一个分区")
        seen = set()
        norm = []
        for z in zones:
            for key in ("zone_id", "name", "floor", "participant_cap", "spectator_cap"):
                if key not in z:
                    raise DomainError(f"分区缺少字段 {key}")
            if z["zone_id"] in seen:
                raise DomainError(f"分区编号重复 {z['zone_id']}")
            seen.add(z["zone_id"])
            for cap_key in ("participant_cap", "spectator_cap"):
                if not isinstance(z[cap_key], int) or z[cap_key] < 0:
                    raise DomainError(f"{z['zone_id']} 的 {cap_key} 必须是非负整数")
            norm.append(
                {
                    "zone_id": z["zone_id"],
                    "name": z["name"],
                    "floor": z["floor"],
                    "participant_cap": z["participant_cap"],
                    "spectator_cap": z["spectator_cap"],
                    "fire_exit": bool(z.get("fire_exit", False)),
                }
            )
        return self._emit(
            "SPACE_VERSION_DRAFTED",
            {
                "space_version": space_version,
                "label": label,
                "effective_from": effective_from,
                "zones": norm,
            },
            now,
        )

    def publish_version(self, space_version, now):
        record = self._require_version(space_version)
        if record["status"] != "DRAFT":
            raise DomainError("只有草稿版本可以发布")
        return self._emit("SPACE_VERSION_PUBLISHED", {"space_version": space_version}, now)

    def retire_version(self, space_version, now):
        record = self._require_version(space_version)
        if record["status"] == "RETIRED":
            raise DomainError("版本已停用")
        return self._emit("SPACE_VERSION_RETIRED", {"space_version": space_version}, now)

    def _require_version(self, space_version):
        if space_version not in self.versions:
            raise DomainError(f"未知空间版本 {space_version}")
        return self.versions[space_version]

    def is_published(self, space_version):
        rec = self.versions.get(space_version)
        return bool(rec and rec["status"] == "PUBLISHED")

    def zone(self, space_version, zone_id):
        rec = self._require_version(space_version)
        for z in rec["zones"]:
            if z["zone_id"] == zone_id:
                return z
        raise DomainError(f"空间版本 {space_version} 中无分区 {zone_id}")

    def when(self, event):
        d = event["data"]
        kind = event["kind"]
        if kind == "SPACE_VERSION_DRAFTED":
            self.versions[d["space_version"]] = {**d, "status": "DRAFT"}
        elif kind == "SPACE_VERSION_PUBLISHED":
            self.versions[d["space_version"]]["status"] = "PUBLISHED"
        elif kind == "SPACE_VERSION_RETIRED":
            self.versions[d["space_version"]]["status"] = "RETIRED"


# ---------------------------------------------------------------- 活动玩法目录


class ActivityCatalog(Aggregate):
    def __init__(self, subject_id):
        super().__init__(subject_id)
        self.activities: dict[str, dict] = {}

    def register_activity(
        self,
        activity_id,
        name,
        gameplay,
        risk_level,
        *,
        team_size_min,
        team_size_max,
        age_min,
        spectators_per_team_max=2,
        safety_requirements=(),
        prizes=(),
        now,
    ):
        if activity_id in self.activities:
            raise DomainError(f"活动 {activity_id} 已注册")
        if risk_level not in RISK_LEVELS:
            raise DomainError(f"风险级别必须是 {RISK_LEVELS}")
        if not (1 <= team_size_min <= team_size_max):
            raise DomainError("队伍人数上下界不合法")
        if risk_level == "HIGH" and not safety_requirements:
            raise DomainError("高风险活动必须列出安全防护要求")
        norm_prizes = []
        for p in prizes:
            if "rank" not in p or "name" not in p:
                raise DomainError("奖品必须包含 rank 与 name")
            norm_prizes.append({"rank": int(p["rank"]), "name": str(p["name"])})
        return self._emit(
            "ACTIVITY_REGISTERED",
            {
                "activity_id": activity_id,
                "name": name,
                "gameplay": gameplay,
                "risk_level": risk_level,
                "team_size_min": team_size_min,
                "team_size_max": team_size_max,
                "age_min": age_min,
                "spectators_per_team_max": spectators_per_team_max,
                "safety_requirements": list(safety_requirements),
                "prizes": sorted(norm_prizes, key=lambda p: p["rank"]),
                "status": "ACTIVE",
            },
            now,
        )

    def update_risk(self, activity_id, risk_level, safety_requirements, now):
        activity = self._require(activity_id)
        if risk_level not in RISK_LEVELS:
            raise DomainError(f"风险级别必须是 {RISK_LEVELS}")
        if risk_level == "HIGH" and not safety_requirements:
            raise DomainError("高风险活动必须列出安全防护要求")
        return self._emit(
            "ACTIVITY_UPDATED",
            {
                "activity_id": activity_id,
                "risk_level": risk_level,
                "safety_requirements": list(safety_requirements),
            },
            now,
        )

    def retire_activity(self, activity_id, now):
        self._require(activity_id)
        return self._emit("ACTIVITY_RETIRED", {"activity_id": activity_id}, now)

    def _require(self, activity_id):
        activity = self.activities.get(activity_id)
        if not activity:
            raise DomainError(f"未知活动 {activity_id}")
        return activity

    def when(self, event):
        d = event["data"]
        if event["kind"] == "ACTIVITY_REGISTERED":
            self.activities[d["activity_id"]] = dict(d)
        elif event["kind"] == "ACTIVITY_UPDATED":
            a = self.activities[d["activity_id"]]
            a["risk_level"] = d["risk_level"]
            a["safety_requirements"] = d["safety_requirements"]
        elif event["kind"] == "ACTIVITY_RETIRED":
            self.activities[d["activity_id"]]["status"] = "RETIRED"


# ---------------------------------------------------------------- 单场活动


class Session(Aggregate):
    """一场活动：排期、冻结检查、报名/候补、签到、入场、分组、成绩、奖品。"""

    def __init__(self, subject_id):
        super().__init__(subject_id)
        self.scheduled = False
        self.frozen = False
        self.signup_open = False
        self.finished = False
        self.checklist = None
        self.registrations: dict[str, dict] = {}
        self.waitlist: list[str] = []
        self.groups: list[dict] = []
        self.results: dict[str, dict] = {}
        self.final_ranking: list[str] = []
        self.awarded: set[str] = set()
        self.change_notices: list[dict] = []

    @property
    def participant_cap(self):
        return self._sched["participant_cap"]

    @property
    def confirmed_count(self):
        return sum(1 for r in self.registrations.values() if r["status"] == "REGISTERED")

    def schedule(self, data, now):
        if self.scheduled:
            raise DomainError("场次已排期")
        required = (
            "store_id", "activity_id", "activity_name", "risk_level", "age_min",
            "space_version", "zone_id", "start_at", "cutoff_at", "end_at",
            "participant_cap", "spectator_cap", "spectators_per_team_max",
            "team_size_min", "team_size_max", "heat_size",
        )
        missing = [k for k in required if k not in data]
        if missing:
            raise DomainError(f"排期缺少字段 {missing}")
        if data["participant_cap"] <= 0:
            raise DomainError("参赛者容量必须为正")
        if parse_ts(data["cutoff_at"]) > parse_ts(data["start_at"]) + timedelta(minutes=30):
            raise DomainError("签到截止时间异常")
        return self._emit("SESSION_SCHEDULED", dict(data), now)

    def freeze(
        self, *, venue_confirmed, staff, referees, emergency_plan_id,
        guardian_station=False, now,
    ):
        if not self.scheduled:
            raise DomainError("尚未排期")
        if self.frozen:
            raise DomainError("预案已冻结")
        s = self._sched
        need_refs = 2 if s["risk_level"] == "HIGH" else 1
        need_staff = 2 if s["risk_level"] == "HIGH" else 1
        missing = []
        if not venue_confirmed:
            missing.append("场地确认")
        if len(staff) < need_staff:
            missing.append(f"现场安全人员不足({need_staff}人)")
        if len(referees) < need_refs:
            missing.append(f"裁判不足({need_refs}人)")
        if not emergency_plan_id:
            missing.append("应急预案")
        if s["age_min"] < 18 and not guardian_station:
            missing.append("未成年人监护核验岗")
        if missing:
            raise DomainError("活动开始前检查未通过：" + "、".join(missing))
        return self._emit(
            "SESSION_FROZEN",
            {
                "venue_confirmed": True,
                "staff": list(staff),
                "referees": list(referees),
                "emergency_plan_id": emergency_plan_id,
                "guardian_station": bool(guardian_station),
            },
            now,
        )

    def open_signup(self, now):
        if not self.frozen:
            raise DomainError("场地、人员与预案未齐备，不能开放报名")
        if self.signup_open:
            raise DomainError("报名已开放")
        return self._emit("SIGNUP_OPENED", {"at": now}, now)

    def register_team(
        self, registration_id, team_name, member_profiles, spectator_count,
        contact, now, *, command_id=None, actor_id=None,
    ):
        if command_id is not None and command_id in self._commands:
            # 重复扫码的同一指令重放：确认事实，不产生任何新事件/名额变动。
            return None
        if not self.signup_open:
            raise DomainError("报名通道未开放")
        if parse_ts(now) >= parse_ts(self._sched["cutoff_at"]):
            # 截止之后的任何消息都不能再制造名额：拒绝新报名。
            raise DomainError("已过报名/签到截止时间")
        if registration_id in self.registrations:
            raise DomainError("该队伍已报名")
        s = self._sched
        size = len(member_profiles)
        if not (s["team_size_min"] <= size <= s["team_size_max"]):
            raise DomainError(
                f"队伍人数须为 {s['team_size_min']}-{s['team_size_max']} 人"
            )
        if spectator_count > s["spectators_per_team_max"] * size:
            raise DomainError("随行围观人数超出限制")
        minor_count = 0
        for profile in member_profiles:
            extra = set(profile) - ALLOWED_PROFILE_KEYS
            if extra:
                # 消费金额、会员等级之类的字段在这里直接被拒绝。
                raise DomainError(f"资格信息含不被接受的字段 {sorted(extra)}")
            if profile.get("age", 0) < s["age_min"]:
                raise DomainError("存在不符合年龄要求的成员")
            if profile.get("age", 0) < 18 and not profile.get("guardian_present"):
                raise DomainError("未成年人参赛须有监护人在场")
            if not profile.get("safety_notice_signed"):
                raise DomainError("全体成员须签署安全告知")
            if profile.get("age", 0) < 18:
                minor_count += 1

        if self.confirmed_count < s["participant_cap"]:
            status, position = "REGISTERED", None
        else:
            status, position = "WAITLISTED", len(self.waitlist) + 1
        return self._emit(
            "TEAM_REGISTERED",
            {
                "registration_id": registration_id,
                "team_name": team_name,
                "size": size,
                "minor_count": minor_count,
                "spectator_count": spectator_count,
                "contact": contact,
                "status": status,
                "waitlist_position": position,
            },
            now,
            command_id=command_id,
            actor_id=actor_id,
        )

    def cancel_registration(self, registration_id, now, *, command_id=None):
        reg = self._reg(registration_id)
        if reg["status"] in ("CANCELLED", "NO_SHOW"):
            # 迟到/重复的取消消息：确认事实但不产生任何名额变动。
            return None
        before = reg["status"]
        events = [
            self._emit(
                "REGISTRATION_CANCELLED",
                {"registration_id": registration_id, "reason": "USER_CANCELLED"},
                now,
                command_id=command_id,
            )
        ]
        # 已出票的队伍退出，票同步回收，占用才能真实下降。
        if reg.get("ticket_id") and not reg.get("released"):
            events.append(self._emit(
                "ADMISSION_RELEASED",
                {"ticket_id": reg["ticket_id"], "registration_id": registration_id,
                 "reason": "REGISTRATION_CANCELLED"},
                now,
            ))
        # 只有截止时间之前的退出才按 FIFO 递补；之后的取消不制造新名额。
        if before == "REGISTERED" and parse_ts(now) < parse_ts(self._sched["cutoff_at"]):
            promoted = self._promote_one(now)
            if promoted:
                events.append(promoted)
        return [e for e in events if e]

    def _promote_one(self, now):
        if not self.waitlist or self.confirmed_count >= self._sched["participant_cap"]:
            return None
        rid = self.waitlist[0]
        return self._emit(
            "WAITLIST_PROMOTED",
            {"registration_id": rid},
            now,
        )

    def check_in(self, registration_id, spectators_present, now, *, command_id=None):
        reg = self._reg(registration_id)
        if reg.get("checked_at"):
            # 重复扫码：签到是幂等的，直接返回已存在的事实。
            return None
        if reg["status"] != "REGISTERED":
            raise DomainError("候补或已取消的队伍不能签到")
        if parse_ts(now) > parse_ts(self._sched["cutoff_at"]):
            raise DomainError("已超过签到截止时间")
        if spectators_present > reg["spectator_count"]:
            raise DomainError("到场围观者超过报名数量")
        return self._emit(
            "TEAM_CHECKED_IN",
            {
                "registration_id": registration_id,
                "spectators_present": spectators_present,
            },
            now,
            command_id=command_id,
        )

    def sweep_no_shows(self, now):
        """签到截止后的唯一缺席处理：确定性批量取消，不做递补。"""
        if parse_ts(now) < parse_ts(self._sched["cutoff_at"]):
            raise DomainError("未到签到截止时间")
        events = []
        for rid, reg in list(self.registrations.items()):
            if reg["status"] == "REGISTERED" and not reg.get("checked_at"):
                events.append(
                    self._emit(
                        "NO_SHOW_CANCELLED",
                        {"registration_id": rid},
                        now,
                    )
                )
        return events

    def admit(self, registration_id, now, *, command_id=None):
        reg = self._reg(registration_id)
        if reg.get("ticket_id"):
            return None  # 断网补签重放：票已存在，不重复占额。
        if not reg.get("checked_at"):
            raise DomainError("未签到不能入场")
        if reg["status"] != "REGISTERED":
            raise DomainError("队伍资格已失效")
        participants = reg["size"]
        spectators = reg["spectators_present"]
        ticket_id = f"T-{self.id.split(':', 1)[-1]}-{registration_id}"
        return self._emit(
            "ADMISSION_TICKETED",
            {
                "session_id": self.id,
                "ticket_id": ticket_id,
                "registration_id": registration_id,
                "participants": participants,
                "spectators": spectators,
            },
            now,
            command_id=command_id,
        )

    def release_ticket(self, ticket_id, reason, now):
        rid = next(
            (r["registration_id"] for r in self.registrations.values()
             if r.get("ticket_id") == ticket_id),
            None,
        )
        if rid is None:
            raise DomainError("未知票号")
        return self._emit(
            "ADMISSION_RELEASED",
            {"ticket_id": ticket_id, "registration_id": rid, "reason": reason},
            now,
        )

    def publish_change(self, change_type, message, now, *, affects=None):
        return self._emit(
            "SESSION_CHANGE_PUBLISHED",
            {
                "change_type": change_type,
                "message": message,
                "affects": affects or "ALL",
                "seq": len(self.change_notices) + 1,
            },
            now,
        )

    def draw_groups(self, now):
        if self.groups:
            raise DomainError("已分组")
        teams = [rid for rid, r in self.registrations.items() if r.get("checked_at")]
        if not teams:
            raise DomainError("没有已签到队伍")
        # 用场次标识做种子：分组结果可复现、可审计。
        random.Random(self.id).shuffle(teams)
        size = self._sched["heat_size"]
        chunks = [teams[i:i + size] for i in range(0, len(teams), size)]
        return self._emit(
            "GROUPS_DRAWN",
            {"groups": [{"group_id": f"G{i + 1}", "team_ids": c} for i, c in enumerate(chunks)]},
            now,
        )

    def record_result(self, group_id, scores, referee_id, now):
        if not self.frozen or referee_id not in self.checklist["referees"]:
            raise DomainError("只有本场登记裁判可以记录成绩")
        group = next((g for g in self.groups if g["group_id"] == group_id), None)
        if group is None:
            raise DomainError("未知小组")
        if set(scores) - set(group["team_ids"]):
            raise DomainError("成绩包含不在该组的队伍")
        ranking = sorted(group["team_ids"], key=lambda rid: -scores[rid])
        return self._emit(
            "MATCH_RESULT_RECORDED",
            {"group_id": group_id, "scores": dict(scores), "ranking": ranking,
             "referee_id": referee_id},
            now,
        )

    def finish(self, now):
        if self.finished:
            raise DomainError("场次已结束")
        totals: dict[str, float] = {}
        for r in self.results.values():
            for rid, score in r["scores"].items():
                totals[rid] = totals.get(rid, 0) + score
        ranking = sorted(totals, key=lambda rid: -totals[rid])
        return self._emit("SESSION_FINISHED", {"ranking": ranking}, now)

    def award_prize(self, registration_id, prize_name, rank, now):
        if not self.finished:
            raise DomainError("场次未结束，不能颁发奖品")
        expected = self.final_ranking[rank - 1] if rank <= len(self.final_ranking) else None
        if registration_id != expected:
            raise DomainError("奖品只能按裁判成绩名次颁发，与消费无关")
        if registration_id in self.awarded:
            raise DomainError("该队伍已领奖")
        return self._emit(
            "PRIZE_AWARDED",
            {"registration_id": registration_id, "prize_name": prize_name, "rank": rank,
             "basis": "RESULT_RANK"},
            now,
        )

    def _reg(self, registration_id):
        reg = self.registrations.get(registration_id)
        if reg is None:
            raise DomainError(f"未知报名 {registration_id}")
        return reg

    def when(self, event):
        d = event["data"]
        k = event["kind"]
        if k == "SESSION_SCHEDULED":
            self.scheduled = True
            self._sched = d
        elif k == "SESSION_FROZEN":
            self.frozen = True
            self.checklist = d
        elif k == "SIGNUP_OPENED":
            self.signup_open = True
        elif k == "TEAM_REGISTERED":
            self.registrations[d["registration_id"]] = {
                "registration_id": d["registration_id"],
                "team_name": d["team_name"],
                "size": d["size"],
                "minor_count": d["minor_count"],
                "spectator_count": d["spectator_count"],
                "spectators_present": 0,
                "contact": d["contact"],
                "status": d["status"],
                "checked_at": None,
                "ticket_id": None,
                "released": False,
            }
            if d["status"] == "WAITLISTED":
                self.waitlist.append(d["registration_id"])
        elif k == "REGISTRATION_CANCELLED":
            self.registrations[d["registration_id"]]["status"] = "CANCELLED"
        elif k == "WAITLIST_PROMOTED":
            rid = d["registration_id"]
            self.registrations[rid]["status"] = "REGISTERED"
            self.waitlist.remove(rid)
            for i, waiting in enumerate(self.waitlist, 1):
                self.registrations[waiting]["waitlist_position"] = i
        elif k == "TEAM_CHECKED_IN":
            reg = self.registrations[d["registration_id"]]
            reg["checked_at"] = event["occurred_at"]
            reg["spectators_present"] = d["spectators_present"]
        elif k == "NO_SHOW_CANCELLED":
            self.registrations[d["registration_id"]]["status"] = "NO_SHOW"
        elif k == "ADMISSION_TICKETED":
            reg = self.registrations[d["registration_id"]]
            reg["ticket_id"] = d["ticket_id"]
        elif k == "ADMISSION_RELEASED":
            reg = self.registrations[d["registration_id"]]
            reg["released"] = True
        elif k == "GROUPS_DRAWN":
            self.groups = d["groups"]
        elif k == "MATCH_RESULT_RECORDED":
            self.results[d["group_id"]] = d
        elif k == "SESSION_FINISHED":
            self.finished = True
            self.final_ranking = d["ranking"]
        elif k == "PRIZE_AWARDED":
            self.awarded.add(d["registration_id"])
        elif k == "SESSION_CHANGE_PUBLISHED":
            self.change_notices.append(d)


# ---------------------------------------------------------------- 分区运行状态


class ZoneOperations(Aggregate):
    """单个分区的暂停/恢复与客流观测。容量计数由投影从票务事件汇总。"""

    def __init__(self, subject_id):
        super().__init__(subject_id)
        self.space_version = None
        self.paused = False
        self.pause_record = None
        self.observations: list[dict] = []

    def activate(self, space_version, now):
        return self._emit("ZONE_ACTIVATED", {"space_version": space_version}, now)

    def pause(self, trigger, detail, arrangement, now, *, expected_duration_min=30):
        if self.paused:
            raise DomainError("分区已在暂停中")
        if trigger not in PAUSE_TRIGGERS:
            raise DomainError(f"暂停原因必须是 {PAUSE_TRIGGERS}")
        actions = {"hold", "transfer_session", "overflow_zone", "reentry_priority"}
        if "message" not in arrangement or not (actions & set(arrangement)):
            raise DomainError("必须为已到场者给出可执行安排（等待/转场/溢出区/优先返场）")
        return self._emit(
            "ZONE_PAUSED",
            {
                "trigger": trigger,
                "detail": detail,
                "arrangement": arrangement,
                "expected_duration_min": expected_duration_min,
            },
            now,
        )

    def resume(self, note, now):
        if not self.paused:
            raise DomainError("分区未暂停")
        return self._emit("ZONE_RESUMED", {"note": note}, now)

    def observe(self, participants_est, spectators_est, source, now):
        if participants_est < 0 or spectators_est < 0:
            raise DomainError("观测人数不能为负")
        return self._emit(
            "CROWD_OBSERVED",
            {
                "participants_est": participants_est,
                "spectators_est": spectators_est,
                "source": source,
            },
            now,
        )

    def when(self, event):
        d = event["data"]
        if event["kind"] == "ZONE_ACTIVATED":
            self.space_version = d["space_version"]
        elif event["kind"] == "ZONE_PAUSED":
            self.paused = True
            self.pause_record = {**d, "at": event["occurred_at"]}
        elif event["kind"] == "ZONE_RESUMED":
            self.paused = False
            self.pause_record = None
        elif event["kind"] == "CROWD_OBSERVED":
            self.observations.append({**d, "at": event["occurred_at"]})


# ---------------------------------------------------------------- 安全事件


class Incident(Aggregate):
    """受伤、未成年人走失等安全事件；敏感材料只对实际处置人员开放。"""

    def __init__(self, subject_id):
        super().__init__(subject_id)
        self.opened = False
        self.status = None
        self.responders: dict[str, str] = {}
        self.media: list[dict] = []
        self.access_log: list[dict] = []

    def report(self, incident_type, summary, zone_id, reporter_id, now, *,
               session_id=None, severity="MEDIUM"):
        if self.opened:
            raise DomainError("事件已建档")
        if incident_type not in INCIDENT_TYPES:
            raise DomainError(f"事件类型必须是 {INCIDENT_TYPES}")
        return self._emit(
            "INCIDENT_REPORTED",
            {
                "incident_type": incident_type,
                "summary": summary,
                "zone_id": zone_id,
                "session_id": session_id,
                "reporter_id": reporter_id,
                "severity": severity,
                "status": "OPEN",
            },
            now,
            actor_id=reporter_id,
        )

    def assign_responder(self, user_id, role, now):
        return self._emit(
            "RESPONDER_ASSIGNED",
            {"responder_id": user_id, "role": role},
            now,
        )

    def change_status(self, status, note, now):
        if status not in ("RESPONDING", "RESOLVED"):
            raise DomainError("未知状态")
        return self._emit(
            "INCIDENT_STATUS_CHANGED", {"status": status, "note": note}, now
        )

    def add_media(self, media_id, uploader_id, now, *, sensitive=True):
        return self._emit(
            "INCIDENT_MEDIA_ADDED",
            {"media_id": media_id, "sensitive": sensitive, "uploader_id": uploader_id},
            now,
            actor_id=uploader_id,
        )

    def mark_minor_reunited(self, guardian_id, verifier_id, method, now):
        return self._emit(
            "MINOR_REUNITED",
            {"guardian_id": guardian_id, "verifier_id": verifier_id, "method": method},
            now,
            actor_id=verifier_id,
        )

    def access_sensitive_media(self, media_id, user_id, now):
        """访问判定：非处置人员直接拒绝，允许访问则留痕。"""
        item = next((m for m in self.media if m["media_id"] == media_id), None)
        if item is None:
            raise DomainError("未知材料")
        if item["sensitive"] and user_id not in self.responders:
            raise AccessDenied("敏感材料仅向实际处置人员开放")
        return self._emit(
            "INCIDENT_MEDIA_ACCESSED",
            {"media_id": media_id, "viewer_id": user_id},
            now,
            actor_id=user_id,
        )

    def when(self, event):
        d = event["data"]
        k = event["kind"]
        if k == "INCIDENT_REPORTED":
            self.opened = True
            self.status = "OPEN"
            self.report_data = d
        elif k == "RESPONDER_ASSIGNED":
            self.responders[d["responder_id"]] = d["role"]
            if self.status == "OPEN":
                self.status = "RESPONDING"
        elif k == "INCIDENT_STATUS_CHANGED":
            self.status = d["status"]
        elif k == "INCIDENT_MEDIA_ADDED":
            self.media.append(d)
        elif k == "INCIDENT_MEDIA_ACCESSED":
            self.access_log.append(d)
        elif k == "MINOR_REUNITED":
            self.reunited = d


# ---------------------------------------------------------------- 拍摄授权与素材


class Consent(Aggregate):
    """个人（含未成年人监护人）拍摄授权；撤回即时生效。"""

    def __init__(self, subject_id):
        super().__init__(subject_id)
        self.person_id = subject_id.split(":", 1)[1]
        self.minor = False
        self.grants: list[dict] = []
        self.withdrawals: list[dict] = []

    def grant(self, scopes, minor, now, *, guardian_id=None, session_id=None):
        if minor and not guardian_id:
            raise DomainError("未成年人授权必须由监护人作出")
        unknown = set(scopes) - {"PHOTO", "VIDEO", "LIVE"}
        if unknown:
            raise DomainError(f"未知授权范围 {sorted(unknown)}")
        return self._emit(
            "CONSENT_GRANTED",
            {"scopes": list(scopes), "minor": minor, "guardian_id": guardian_id,
             "session_id": session_id},
            now,
            actor_id=guardian_id,
        )

    def withdraw(self, scopes, now):
        if not self.grants:
            raise DomainError("尚无授权可撤回")
        return self._emit("CONSENT_WITHDRAWN", {"scopes": list(scopes)}, now)

    def licensed_for(self, scope, at):
        """某时刻某范围是否有有效授权：按时间回放，最近一次授予/撤回生效。"""
        moment = parse_ts(at)
        actions = [("grant", parse_ts(g["at"])) for g in self.grants if scope in g["scopes"]]
        actions += [("withdraw", parse_ts(w["at"]))
                    for w in self.withdrawals if scope in w["scopes"]]
        allowed = False
        for action, ts in sorted(actions, key=lambda x: x[1]):
            if ts <= moment:
                allowed = action == "grant"
        return allowed

    def when(self, event):
        d = event["data"]
        if event["kind"] == "CONSENT_GRANTED":
            self.minor = d["minor"]
            self.grants.append({**d, "at": event["occurred_at"]})
        elif event["kind"] == "CONSENT_WITHDRAWN":
            self.withdrawals.append({**d, "at": event["occurred_at"]})


class MediaAsset(Aggregate):
    def __init__(self, subject_id):
        super().__init__(subject_id)
        self.captured = False
        self.block_reasons: list[str] = []
        self.takedown_reason = None
        self.taken_down = False

    def capture(self, session_id, zone_id, captured_at, subjects, scope, now, *,
                sensitive=False, incident_id=None):
        if self.captured:
            raise DomainError("素材已登记")
        if scope not in ("PHOTO", "VIDEO", "LIVE"):
            raise DomainError("未知素材类型")
        return self._emit(
            "MEDIA_CAPTURED",
            {
                "session_id": session_id,
                "zone_id": zone_id,
                "captured_at": captured_at,
                "subjects": list(subjects),
                "scope": scope,
                "sensitive": sensitive,
                "incident_id": incident_id,
            },
            now,
        )

    def block(self, reasons, now):
        return self._emit("MEDIA_PUBLISH_BLOCKED", {"reasons": list(reasons)}, now)

    def request_takedown(self, reason, now):
        return self._emit("MEDIA_TAKEDOWN_REQUESTED", {"reason": reason}, now)

    def confirm_takedown(self, by, now):
        return self._emit("MEDIA_TAKEN_DOWN", {"by": by}, now, actor_id=by)

    def when(self, event):
        d = event["data"]
        k = event["kind"]
        if k == "MEDIA_CAPTURED":
            self.captured = True
            self.info = d
        elif k == "MEDIA_PUBLISH_BLOCKED":
            self.block_reasons.extend(d["reasons"])
        elif k == "MEDIA_TAKEDOWN_REQUESTED":
            self.takedown_reason = d["reason"]
        elif k == "MEDIA_TAKEN_DOWN":
            self.taken_down = True


# ---------------------------------------------------------------- 商户协作


class MerchantBoard(Aggregate):
    def __init__(self, subject_id):
        super().__init__(subject_id)
        self.tasks: dict[str, dict] = {}

    def create_task(self, task_id, merchant_id, task_type, description, due_at,
                    session_id, now):
        if task_id in self.tasks:
            raise DomainError("任务已存在")
        if task_type not in ("QUEUE_SUPPORT", "PRIZE_SPONSOR", "EQUIPMENT", "STAFF_SUPPORT"):
            raise DomainError("未知协作类型")
        return self._emit(
            "MERCHANT_TASK_CREATED",
            {
                "task_id": task_id,
                "merchant_id": merchant_id,
                "task_type": task_type,
                "description": description,
                "due_at": due_at,
                "session_id": session_id,
                "status": "OPEN",
            },
            now,
        )

    def update_status(self, task_id, status, note, now):
        task = self.tasks.get(task_id)
        if task is None:
            raise DomainError("未知任务")
        if status not in ("ACCEPTED", "IN_PROGRESS", "DONE", "CANCELLED"):
            raise DomainError("未知状态")
        return self._emit(
            "MERCHANT_TASK_STATUS_CHANGED",
            {"task_id": task_id, "status": status, "note": note},
            now,
        )

    def when(self, event):
        d = event["data"]
        if event["kind"] == "MERCHANT_TASK_CREATED":
            self.tasks[d["task_id"]] = dict(d)
        elif event["kind"] == "MERCHANT_TASK_STATUS_CHANGED":
            self.tasks[d["task_id"]]["status"] = d["status"]


# ---------------------------------------------------------------- 体验度量


class ExperienceLog(Aggregate):
    """体验与停留样本。结构上只接受体验指标，不接受消费金额。"""

    def __init__(self, subject_id):
        super().__init__(subject_id)
        self.samples: list[dict] = []

    def sample(self, rating, dwell_minutes, source, now, *, session_id=None):
        if not (1 <= rating <= 5):
            raise DomainError("评分取值 1-5")
        if dwell_minutes < 0:
            raise DomainError("停留时长不能为负")
        return self._emit(
            "EXPERIENCE_SAMPLED",
            {"rating": rating, "dwell_minutes": dwell_minutes, "source": source,
             "session_id": session_id},
            now,
        )

    def when(self, event):
        if event["kind"] == "EXPERIENCE_SAMPLED":
            self.samples.append({**event["data"], "at": event["occurred_at"]})
