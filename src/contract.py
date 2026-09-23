"""事件信封约定。

所有服务沿用同一标识与时间语义：

* ``event_id``    事件全局唯一编号（通常含日期与序号）。
* ``kind``        事件类型，大写下划线。
* ``occurred_at`` 事件实际发生时刻（ISO-8601，带时区）。
* ``subject_id``  事件流 / 聚合标识，同一 subject 的 version 严格递增。
* ``version``     该事件在 subject 流内的序号，从 1 开始。
* ``command_id``  产生该事件的命令幂等键，同一命令重放不得产生第二个事件。
* ``actor_id``    发令操作人（店员 / 裁判 / 顾客 / 系统）。
* ``data``        事件负载，结构由 kind 决定。
"""

from datetime import datetime

REQUIRED = ("event_id", "kind", "occurred_at", "subject_id", "version")


def validate(record: dict) -> list[str]:
    """仅检查基线必填字段，保持与既有调用方兼容。"""
    return [name for name in REQUIRED if name not in record]


def validate_envelope(record: dict) -> list[str]:
    """完整信封校验，返回错误信息列表，空列表表示通过。"""
    errors = [f"缺少字段 {name}" for name in REQUIRED if name not in record]
    if errors:
        return errors

    if not isinstance(record["kind"], str) or not record["kind"]:
        errors.append("kind 必须是非空字符串")
    if not isinstance(record["subject_id"], str) or not record["subject_id"]:
        errors.append("subject_id 必须是非空字符串")
    if not isinstance(record["version"], int) or record["version"] < 1:
        errors.append("version 必须是 >=1 的整数")
    try:
        datetime.fromisoformat(record["occurred_at"])
    except (TypeError, ValueError):
        errors.append("occurred_at 必须是 ISO-8601 时间")
    if "data" in record and not isinstance(record["data"], dict):
        errors.append("data 必须是对象")
    if "command_id" in record and not isinstance(record["command_id"], str):
        errors.append("command_id 必须是字符串")
    return errors


def parse_ts(value: str) -> datetime:
    """统一的时间解析入口，强制带时区，避免跨门店歧义。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("时间必须带时区偏移")
    return dt
