"""事件存储：按 subject 流保存、乐观并发、命令幂等。

存储本身不做业务判断，只保证两件事：

1. 同一 subject 的 version 严格连续递增（并发写入用 expected_version 冲突）。
2. 同一 command_id 只接受一次——重复扫码、断网补签、迟到的取消消息
   重放时返回原事件，而不是再制造一个名额变动。
"""

from dataclasses import dataclass, field

from .contract import parse_ts, validate_envelope


class ConcurrentStreamError(RuntimeError):
    """expected_version 与流尾不一致时抛出。"""


@dataclass
class EventStore:
    _streams: dict[str, list[dict]] = field(default_factory=dict)
    _commands: dict[str, str] = field(default_factory=dict)

    def append(self, event: dict, *, expected_version: int | None = None) -> dict:
        errors = validate_envelope(event)
        if errors:
            raise ValueError("; ".join(errors))

        subject = event["subject_id"]
        stream = self._streams.setdefault(subject, [])
        current = len(stream)
        if expected_version is not None and current != expected_version:
            raise ConcurrentStreamError(
                f"{subject}: 期望版本 {expected_version}，实际 {current}"
            )
        if event["version"] != current + 1:
            raise ConcurrentStreamError(
                f"{subject}: 事件版本 {event['version']} 应为 {current + 1}"
            )

        command_id = event.get("command_id")
        if command_id is not None:
            existing_subject = self._commands.get(command_id)
            if existing_subject is not None:
                # 幂等重放：返回首次写入的事件，绝不新增。
                original = next(
                    e for e in self._streams[existing_subject]
                    if e.get("command_id") == command_id
                )
                return original
            self._commands[command_id] = subject

        parse_ts(event["occurred_at"])
        stream.append(event)
        return event

    def read_stream(self, subject_id: str) -> list[dict]:
        return list(self._streams.get(subject_id, []))

    def read_all(self) -> list[dict]:
        events = [e for stream in self._streams.values() for e in stream]
        return sorted(events, key=lambda e: (parse_ts(e["occurred_at"]), e["event_id"]))

    def stream_version(self, subject_id: str) -> int:
        return len(self._streams.get(subject_id, []))
