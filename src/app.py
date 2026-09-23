"""应用服务：命令入口与跨聚合规则。

聚合只管自己那条流；涉及多个聚合的判断在这里：

* 入场前用空间版本的容量做分区级硬校验（参赛者/围观者分开）。
* 消防通道或邻铺拥堵只暂停受影响分区，并向该分区内的场次发布
  可执行安排，已出票队伍的资格不被取消。
* 素材发布前逐人核验授权，未成年人撤回与安全事件敏感材料一律拦截。
"""

from . import domain
from .domain import (
    ActivityCatalog, Consent, ExperienceLog, Incident, MediaAsset, MerchantBoard,
    Session, SpaceLayout, ZoneOperations,
)
from .projections import (
    ConsentDirectory, CustomerBoardView, ExperienceMetrics, OccupancyView, replay,
)
from .store import EventStore


class Application:
    def __init__(self, store: EventStore | None = None):
        self.store = store or EventStore()
        self.occupancy = OccupancyView()
        self.board = CustomerBoardView()
        self.consents = ConsentDirectory()
        self.metrics = ExperienceMetrics()
        replay(self.store, self.occupancy, self.board, self.consents, self.metrics)

    # ------------------------------------------------------------ 持久化

    def _commit(self, agg, produced):
        if produced is None:
            return []
        events = produced if isinstance(produced, list) else [produced]
        saved = []
        for event in events:
            # 版本连续性由 store 以 event.version 校验，等价于乐观并发。
            saved.append(self.store.append(event))
            self.occupancy.handle(event)
            self.board.handle(event)
            self.consents.handle(event)
            self.metrics.handle(event)
        return saved

    def _load(self, cls, subject_id):
        return cls.load(subject_id, self.store.read_stream(subject_id))

    # ------------------------------------------------------------ 空间与目录

    def draft_space(self, store_id, space_version, label, zones, effective_from, now):
        agg = self._load(SpaceLayout, f"space:{store_id}")
        return self._commit(agg, agg.draft_version(
            space_version, label, zones, effective_from, now))

    def publish_space(self, store_id, space_version, now):
        agg = self._load(SpaceLayout, f"space:{store_id}")
        return self._commit(agg, agg.publish_version(space_version, now))

    def register_activity(self, **kwargs):
        agg = self._load(ActivityCatalog, "catalog")
        return self._commit(agg, agg.register_activity(**kwargs))

    # ------------------------------------------------------------ 场次全流程

    def schedule_session(self, store_id, session_id, data, now):
        agg = self._load(Session, f"session:{store_id}:{session_id}")
        data = {"session_id": session_id, "store_id": store_id, **data}
        return self._commit(agg, agg.schedule(data, now))

    def freeze_session(self, store_id, session_id, now, **checks):
        agg = self._session(store_id, session_id)
        return self._commit(agg, agg.freeze(now=now, **checks))

    def open_signup(self, store_id, session_id, now):
        agg = self._session(store_id, session_id)
        return self._commit(agg, agg.open_signup(now))

    def register_team(self, store_id, session_id, now, **kwargs):
        agg = self._session(store_id, session_id)
        return self._commit(
            agg,
            agg.register_team(now=now, **kwargs),
        )

    def cancel_registration(self, store_id, session_id, registration_id, now, *,
                            command_id=None):
        agg = self._session(store_id, session_id)
        return self._commit(
            agg,
            agg.cancel_registration(registration_id, now, command_id=command_id),
        )

    def check_in(self, store_id, session_id, registration_id, spectators_present,
                 now, *, command_id=None):
        agg = self._session(store_id, session_id)
        return self._commit(
            agg,
            agg.check_in(registration_id, spectators_present, now, command_id=command_id),
        )

    def admit(self, store_id, session_id, registration_id, now, *, command_id=None):
        agg = self._session(store_id, session_id)
        if not agg.registrations[registration_id].get("ticket_id"):
            self._guard_zone_capacity(store_id, agg, registration_id)
        return self._commit(
            agg,
            agg.admit(registration_id, now, command_id=command_id),
        )

    def _guard_zone_capacity(self, store_id, session, registration_id):
        sched = session._sched
        space = self._load(SpaceLayout, f"space:{store_id}")
        zone = space.zone(sched["space_version"], sched["zone_id"])
        current = self.occupancy.for_zone(store_id, zone["zone_id"])
        reg = session.registrations[registration_id]
        if current["participants"] + reg["size"] > zone["participant_cap"]:
            raise domain.DomainError("分区参赛者容量已满，暂缓入场")
        if current["spectators"] + reg["spectators_present"] > zone["spectator_cap"]:
            raise domain.DomainError("分区围观容量已满，引导至溢出观赛区")

    def sweep_no_shows(self, store_id, session_id, now):
        agg = self._session(store_id, session_id)
        return self._commit(agg, agg.sweep_no_shows(now))

    def draw_groups(self, store_id, session_id, now):
        agg = self._session(store_id, session_id)
        return self._commit(agg, agg.draw_groups(now))

    def record_result(self, store_id, session_id, group_id, scores, referee_id, now):
        agg = self._session(store_id, session_id)
        return self._commit(
            agg, agg.record_result(group_id, scores, referee_id, now))

    def finish_session(self, store_id, session_id, now):
        agg = self._session(store_id, session_id)
        return self._commit(agg, agg.finish(now))

    def award_prize(self, store_id, session_id, registration_id, prize_name, rank, now):
        agg = self._session(store_id, session_id)
        return self._commit(
            agg, agg.award_prize(registration_id, prize_name, rank, now))

    def publish_change(self, store_id, session_id, change_type, message, now):
        agg = self._session(store_id, session_id)
        return self._commit(agg, agg.publish_change(change_type, message, now))

    def session_queue(self, store_id, session_id, now=None):
        """顾客可见的真实排队：剩余名额、候补长度、已签到数。

        ``accepting`` 明确告诉顾客名额是否还可被填补：签到截止之后，
        缺席名额只是空着，不会再对任何人开放。
        """
        agg = self._session(store_id, session_id)
        accepting = True
        if now is not None:
            from .contract import parse_ts
            accepting = parse_ts(now) < parse_ts(agg._sched["cutoff_at"])
        return {
            "session_id": session_id,
            "open_slots": max(agg.participant_cap - agg.confirmed_count, 0) if accepting else 0,
            "accepting": accepting,
            "waitlist_len": len(agg.waitlist),
            "checked_in": sum(
                1 for r in agg.registrations.values()
                if r.get("checked_at") and r["status"] == "REGISTERED"),
        }

    def customer_board(self, store_id, now=None):
        queues = {}
        for stream_id, sched in self.occupancy.sessions.items():
            if sched["store_id"] != store_id:
                continue
            local_id = stream_id.split(":", 2)[2]
            queues[stream_id] = self.session_queue(store_id, local_id, now=now)
        return [row for row in self.board.board(queues)
                if self.occupancy.sessions[row["session_id"]]["store_id"] == store_id]

    # ------------------------------------------------------------ 分区运行与应急

    def activate_zone(self, store_id, zone_id, space_version, now):
        agg = self._load(ZoneOperations, f"zoneops:{store_id}:{zone_id}")
        return self._commit(agg, agg.activate(space_version, now))

    def pause_zone(self, store_id, zone_id, trigger, detail, arrangement, now,
                   expected_duration_min=30):
        """只暂停受影响分区，并给该分区内在场场次的顾客发出安排。"""
        zone_agg = self._load(ZoneOperations, f"zoneops:{store_id}:{zone_id}")
        if zone_agg.paused:
            return []  # 重复暂停消息：事实已存在，不制造新事件。
        produced = [zone_agg.pause(
            trigger, detail, arrangement, now,
            expected_duration_min=expected_duration_min)]

        for sid, sched in self.occupancy.sessions.items():
            if sched["store_id"] != store_id or sched["zone_id"] != zone_id:
                continue
            session = self._load(Session, sid)
            if session.finished:
                continue
            produced.append(session.publish_change(
                "ZONE_PAUSED",
                f"本区域因{detail}暂停，安排：{arrangement['message']}",
                now,
                affects="ZONE",
            ))
        return self._commit(zone_agg, produced)

    def resume_zone(self, store_id, zone_id, note, now):
        zone_agg = self._load(ZoneOperations, f"zoneops:{store_id}:{zone_id}")
        if not zone_agg.paused:
            return []
        produced = [zone_agg.resume(note, now)]
        for sid, sched in self.occupancy.sessions.items():
            if sched["store_id"] != store_id or sched["zone_id"] != zone_id:
                continue
            session = self._load(Session, sid)
            if session.finished:
                continue
            produced.append(session.publish_change(
                "ZONE_RESUMED", "区域已恢复，持原票可按通知时间返场", now,
                affects="ZONE"))
        return self._commit(zone_agg, produced)

    def observe_crowd(self, store_id, zone_id, participants_est, spectators_est,
                      source, now):
        agg = self._load(ZoneOperations, f"zoneops:{store_id}:{zone_id}")
        return self._commit(agg, agg.observe(
            participants_est, spectators_est, source, now))

    def report_incident(self, incident_id, **kwargs):
        agg = self._load(Incident, f"incident:{incident_id}")
        return self._commit(agg, agg.report(**kwargs))

    def assign_responder(self, incident_id, user_id, role, now):
        agg = self._load(Incident, f"incident:{incident_id}")
        return self._commit(agg, agg.assign_responder(user_id, role, now))

    def add_incident_media(self, incident_id, media_id, uploader_id, now, *,
                           sensitive=True):
        agg = self._load(Incident, f"incident:{incident_id}")
        return self._commit(agg, agg.add_media(
            media_id, uploader_id, now, sensitive=sensitive))

    def access_incident_media(self, incident_id, media_id, user_id, now):
        agg = self._load(Incident, f"incident:{incident_id}")
        return self._commit(agg, agg.access_sensitive_media(media_id, user_id, now))

    def reunite_minor(self, incident_id, guardian_id, verifier_id, method, now):
        agg = self._load(Incident, f"incident:{incident_id}")
        return self._commit(
            agg, agg.mark_minor_reunited(guardian_id, verifier_id, method, now))

    # ------------------------------------------------------------ 授权与素材

    def grant_consent(self, person_id, scopes, now, *, minor=False,
                      guardian_id=None, session_id=None):
        agg = self._load(Consent, f"consent:{person_id}")
        return self._commit(agg, agg.grant(
            scopes, minor, now, guardian_id=guardian_id, session_id=session_id))

    def withdraw_consent(self, person_id, scopes, now):
        agg = self._load(Consent, f"consent:{person_id}")
        events = self._commit(agg, agg.withdraw(scopes, now))
        # 撤回即时生效：已登记素材若含此人且面向发布，进入下架流程由人工确认。
        return events

    def capture_media(self, asset_id, session_id, zone_id, captured_at, subjects,
                      scope, now, *, sensitive=False, incident_id=None):
        agg = self._load(MediaAsset, f"media:{asset_id}")
        return self._commit(agg, agg.capture(
            session_id, zone_id, captured_at, subjects, scope, now,
            sensitive=sensitive, incident_id=incident_id))

    def request_takedown(self, asset_id, reason, now):
        agg = self._load(MediaAsset, f"media:{asset_id}")
        return self._commit(agg, agg.request_takedown(reason, now))

    def confirm_takedown(self, asset_id, by, now):
        agg = self._load(MediaAsset, f"media:{asset_id}")
        return self._commit(agg, agg.confirm_takedown(by, now))

    def publish_decision(self, asset_id, now):
        """素材能否对外发布：以“发布时刻”的授权状态逐人核验。

        撤回即时生效——即便拍摄时授权有效，发布时已撤回也必须拦截。
        未成年人撤回与安全事件敏感材料一律拦截。
        """
        media = self._load(MediaAsset, f"media:{asset_id}")
        if not media.captured:
            raise domain.DomainError("素材未登记")
        info = media.info
        reasons = []
        if media.taken_down:
            reasons.append("ALREADY_TAKEN_DOWN")
        if info["sensitive"] and info.get("incident_id"):
            reasons.append("SENSITIVE_INCIDENT_MATERIAL")
        for person_id in info["subjects"]:
            consent = self.consents.for_person(person_id)
            scope_granted = any(
                info["scope"] in g["scopes"] for g in consent.grants)
            if not consent.grants or not scope_granted:
                reasons.append(f"NO_CONSENT:{person_id}")
            elif not consent.licensed_for(info["scope"], now):
                tag = "MINOR_CONSENT_WITHDRAWN" if consent.minor else "CONSENT_WITHDRAWN"
                reasons.append(f"{tag}:{person_id}")
        return {"asset_id": asset_id, "allowed": not reasons, "reasons": reasons}

    def block_if_unlicensed(self, asset_id, now):
        decision = self.publish_decision(asset_id, now)
        if not decision["allowed"]:
            agg = self._load(MediaAsset, f"media:{asset_id}")
            self._commit(agg, agg.block(decision["reasons"], now))
        return decision

    # ------------------------------------------------------------ 商户与度量

    def create_merchant_task(self, board_id, **kwargs):
        agg = self._load(MerchantBoard, f"board:{board_id}")
        return self._commit(agg, agg.create_task(**kwargs))

    def update_merchant_task(self, board_id, task_id, status, note, now):
        agg = self._load(MerchantBoard, f"board:{board_id}")
        return self._commit(agg, agg.update_status(task_id, status, note, now))

    def sample_experience(self, store_id, session_id, rating, dwell_minutes,
                          source, now):
        agg = self._load(ExperienceLog, f"experience:{store_id}:{session_id}")
        return self._commit(
            agg,
            agg.sample(rating, dwell_minutes, source, now,
                       session_id=f"session:{store_id}:{session_id}"))

    def compare_experience(self, session_ids):
        return self.metrics.compare(session_ids)

    def _session(self, store_id, session_id):
        return self._load(Session, f"session:{store_id}:{session_id}")
