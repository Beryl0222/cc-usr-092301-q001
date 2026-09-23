"""趣味赛事运行系统——事件目录与信封校验。

所有服务沿用基线确认的共同标识与时间语义：

* ``event_id``   全局唯一、幂等（同 id 重放只生效一次）
* ``kind``       本文件目录中登记的事件类型
* ``occurred_at`` 业务发生时间，ISO-8601 带时区；断网补签也填写*实际发生*时刻
* ``subject_id`` 聚合主体（绝大多数为场次 ``session_id``，全局配置为区域/门店 id）
* ``version``    同一 subject_id 内单调递增的序号，用于乐观并发与重排检测
* ``payload``    事件事实（见 ``KINDS`` 的字段声明）
* ``actor_id``   可选，触发人（员工/裁判/响应人/顾客端）

关键领域原则（由下游 projection/policy 强制执行，这里只登记事实）：

* 容量分两类账：参赛者 competitor 与围观者 spectator，不混算。
* 资格不引用消费金额；目录中不存在任何以消费为条件的字段。
* 取消是否释放名额由场次截止与到场状态决定，事件本身只陈述事实。
"""
from __future__ import annotations

from .contract import REQUIRED as ENVELOPE_REQUIRED

VERSION = 1

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")

# 每种事件 payload 的必填字段。选填字段不在此约束，保持前向兼容。
# 顺序即业务生命周期，方便评审通读。
KINDS: dict[str, tuple[str, ...]] = {
    # —— 空间版本：门店场地以"版本"固化，冻结后不得就地改图 ——
    "VENUE_VERSION_PUBLISHED": (
        "venue_version_id", "store_id", "floor", "zones",
    ),
    # zones: [{zone_id, name, role(playing|queuing|spectator|egress|service),
    #          competitor_capacity, spectator_capacity, adjacent_zone_ids[]}]

    # —— 玩法与风险级别 ——
    "GAME_REGISTERED": (
        "game_id", "name", "risk_level", "team_size_min", "team_size_max",
        "age_min", "age_max", "guardian_required",
    ),
    "GAME_SCHEDULED": (
        "session_id", "game_id", "venue_version_id", "zone_ids",
        "slot_start", "slot_end", "competitor_capacity", "spectator_capacity",
        "roster_deadline", "late_grace_minutes",
    ),

    # —— 报名资格（规则只描述年龄/监护/健康等，禁止消费门槛） ——
    "ELIGIBILITY_RULE_SET": ("session_id", "rules"),
    "TEAM_REGISTERED": ("team_id", "session_id", "members"),
    # members: [{member_id, is_minor, guardian_id(未成年必填)}]
    "REGISTRATION_ACCEPTED": ("team_id", "session_id"),
    "REGISTRATION_REJECTED": ("team_id", "session_id", "reason"),
    "WAITLIST_ENTERED": ("team_id", "session_id", "position"),
    "WAITLIST_PROMOTED": ("team_id", "session_id", "reason"),

    # —— 分时入场 ——
    "ENTRY_SLOT_ASSIGNED": ("team_id", "session_id", "admit_from", "admit_until"),
    "ENTRY_SLOT_RESCHEDULED": (
        "team_id", "session_id", "admit_from", "admit_until", "reason",
    ),

    # —— 现场扫码签到（含重复扫码与断网补签） ——
    "CHECKIN_SCANNED": (
        "scan_id", "session_id", "zone_id", "team_id", "member_id",
        "scanned_at", "scanner_id", "offline", "client_record_id",
    ),
    "CHECKIN_DEDUP_REJECTED": ("client_record_id", "session_id", "reason"),
    "OFFLINE_BATCH_SYNCED": ("batch_id", "scanner_id", "records"),

    # —— 迟到、取消（迟到/不到的取消不制造新名额） ——
    "LATE_ARRIVAL_MARKED": ("team_id", "session_id", "marked_at"),
    "TEAM_CANCELLED": ("team_id", "session_id", "cancelled_at", "had_checked_in"),
    "SLOT_FORFEITED": ("team_id", "session_id", "forfeit_at", "reason"),

    # —— 现场分组与裁判 ——
    "BRACKET_BUILT": ("session_id", "groups"),
    # groups: [{group_id, zone_id, team_ids[], heat_no}]
    "HEAT_CALLED": ("session_id", "group_id", "called_at"),
    "MATCH_RESULT_RECORDED": (
        "match_id", "session_id", "group_id", "referee_id", "rankings", "recorded_at",
    ),
    # rankings: [{team_id, rank, score}]
    "RESULT_AMENDED": (
        "match_id", "amendment_id", "referee_id", "rankings", "reason", "amended_at",
    ),

    # —— 奖品 ——
    "PRIZE_DEFINED": ("prize_id", "session_id", "basis", "description"),
    # basis: PARTICIPATION | RANK
    "PRIZE_AWARDED": ("prize_id", "team_id", "awarded_by", "awarded_at"),

    # —— 商户协作与顾客可见排队信息 ——
    "MERCHANT_TASK_ASSIGNED": (
        "task_id", "merchant_id", "zone_id", "kind", "window_start", "window_end",
    ),
    "MERCHANT_TASK_CONFIRMED": ("task_id", "merchant_id", "handled_by"),
    "QUEUE_NOTICE_PUBLISHED": ("zone_id", "expected_wait_minutes", "observed_at"),
    "SESSION_CHANGE_PUBLISHED": ("session_id", "change_type", "message", "visible_at"),

    # —— 客流观测（参赛者/围观者两本账，含离场用于停留时长） ——
    "CROWD_OBSERVED": (
        "zone_id", "observed_at", "competitors_present", "spectators_present",
        "queue_length", "source",
    ),
    "CROWD_THRESHOLD_CROSSED": (
        "zone_id", "metric", "observed_value", "threshold", "direction", "observed_at",
    ),
    "ZONE_ENTRY_OBSERVED": ("zone_id", "member_id", "audience", "observed_at"),
    "ZONE_EXIT_OBSERVED": ("zone_id", "member_id", "audience", "observed_at"),

    # —— 应急：只暂停受影响区域，并只为已到场者安排 ——
    "AREA_SUSPENSION_STARTED": (
        "suspension_id", "zone_ids", "reason", "issued_by", "started_at",
    ),
    "SUSPENSION_ARRANGEMENT_ISSUED": (
        "suspension_id", "team_ids", "action", "instruction", "issued_at",
    ),
    # action: RELOCATE | WAIT_ON_SITE | DEFER_TO_SLOT
    "AREA_SUSPENSION_LIFTED": ("suspension_id", "ended_at"),

    # —— 安全事件（受伤、未成年人走失）与处置人 ——
    "INCIDENT_REPORTED": (
        "incident_id", "zone_id", "kind", "severity", "reported_by", "reported_at",
    ),
    # kind: INJURY | MINOR_LOST | OTHER
    "RESPONDER_ASSIGNED": ("incident_id", "responder_id", "role", "assigned_at"),
    "MINOR_REUNITED": ("incident_id", "member_id", "guardian_id", "verified_by", "at"),
    "INCIDENT_STATUS_CHANGED": ("incident_id", "status", "at"),
    "INCIDENT_RESOLVED": ("incident_id", "resolution", "resolved_at"),

    # —— 素材授权（未成年人、撤回、敏感材料最小可见） ——
    "MEDIA_CONSENT_GRANTED": (
        "consent_id", "member_id", "scope", "granted_by", "granted_at",
    ),
    # scope: [ONSITE_DISPLAY | PUBLIC_PROMOTION | INCIDENT_RECORD]
    "MEDIA_CONSENT_WITHDRAWN": ("consent_id", "member_id", "withdrawn_at"),
    "MEDIA_ASSET_CAPTURED": (
        "asset_id", "zone_id", "captured_at", "consent_ids", "incident_id", "sensitive",
    ),
    "ASSET_ACCESS_GRANTED": (
        "asset_id", "incident_id", "responder_id", "purpose", "granted_at", "expires_at",
    ),
    "ASSET_ACCESS_REVOKED": ("asset_id", "responder_id", "revoked_at"),
    "MEDIA_RELEASE_REQUESTED": (
        "request_id", "asset_id", "channel", "requested_by", "requested_at",
    ),
    "MEDIA_RELEASE_DECIDED": (
        "request_id", "decision", "decided_by", "decided_at", "reason",
    ),

    # —— 开场前就绪确认与冻结（基线 PLAN_FROZEN 沿用） ——
    "READINESS_CHECK_STARTED": ("session_id", "started_at"),
    "READINESS_ITEM_CONFIRMED": (
        "session_id", "area", "item", "confirmed_by", "confirmed_at",
    ),
    # area: VENUE | STAFFING | EMERGENCY_PLAN
    "PLAN_FROZEN": ("frozen_at",),
    "PLAN_UNFROZEN": ("reason", "unfrozen_at"),
}

# 冻结之后仍允许出现的事件类型（运行态事件）；其余结构性变更必须先解冻。
POST_FREEZE_ALLOWED = {
    "CHECKIN_SCANNED", "CHECKIN_DEDUP_REJECTED", "OFFLINE_BATCH_SYNCED",
    "LATE_ARRIVAL_MARKED", "TEAM_CANCELLED", "SLOT_FORFEITED",
    "WAITLIST_PROMOTED", "ENTRY_SLOT_RESCHEDULED",
    "BRACKET_BUILT", "HEAT_CALLED", "MATCH_RESULT_RECORDED", "RESULT_AMENDED",
    "PRIZE_DEFINED", "PRIZE_AWARDED",
    "MERCHANT_TASK_ASSIGNED", "MERCHANT_TASK_CONFIRMED",
    "QUEUE_NOTICE_PUBLISHED", "SESSION_CHANGE_PUBLISHED",
    "CROWD_OBSERVED", "CROWD_THRESHOLD_CROSSED",
    "ZONE_ENTRY_OBSERVED", "ZONE_EXIT_OBSERVED",
    "AREA_SUSPENSION_STARTED", "SUSPENSION_ARRANGEMENT_ISSUED",
    "AREA_SUSPENSION_LIFTED",
    "INCIDENT_REPORTED", "RESPONDER_ASSIGNED", "MINOR_REUNITED",
    "INCIDENT_STATUS_CHANGED", "INCIDENT_RESOLVED",
    "MEDIA_CONSENT_GRANTED", "MEDIA_CONSENT_WITHDRAWN", "MEDIA_ASSET_CAPTURED",
    "ASSET_ACCESS_GRANTED", "ASSET_ACCESS_REVOKED",
    "MEDIA_RELEASE_REQUESTED", "MEDIA_RELEASE_DECIDED",
    "READINESS_ITEM_CONFIRMED", "PLAN_UNFROZEN",
}


def validate_envelope(record: dict) -> list[str]:
    """信封层校验：必填、类型、时间格式、目录登记。"""
    errors: list[str] = []
    errors.extend(f"missing:{name}" for name in ENVELOPE_REQUIRED if name not in record)
    if errors:
        return errors

    if not isinstance(record["event_id"], str) or not record["event_id"]:
        errors.append("bad:event_id")
    if record.get("kind") not in KINDS:
        errors.append(f"unknown_kind:{record.get('kind')}")
    if not isinstance(record["subject_id"], str) or not record["subject_id"]:
        errors.append("bad:subject_id")
    if not isinstance(record["version"], int) or isinstance(record["version"], bool):
        errors.append("bad:version")
    if not _iso_datetime(record.get("occurred_at", "")):
        errors.append("bad:occurred_at")
    return errors


def validate_event(record: dict) -> list[str]:
    """信封 + payload 必填字段校验。"""
    errors = validate_envelope(record)
    if any(e.startswith(("missing:", "unknown_kind")) for e in errors):
        return errors
    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        errors.append("bad:payload")
        return errors
    for field in KINDS[record["kind"]]:
        if field not in payload:
            errors.append(f"missing_payload:{field}")
    return errors


def _iso_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        from datetime import datetime

        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False
