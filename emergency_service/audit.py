import json

from .clock import parse_time, utc_now
from .models import AuditEntry

class AuditLog:
    def __init__(self):
        self.entries = []
        self._sequence = 0
        self._sink = None

    def set_sink(self, sink):
        """每条新审计写入后回调（用于持久化）。"""
        self._sink = sink

    def append(self, action, entity, entity_id, request_id, detail, actor=None):
        self._sequence += 1
        actor = actor or getattr(self, "actor", "system")
        entry = AuditEntry(self._sequence, actor, action, entity, entity_id, request_id, dict(detail), utc_now())
        self.entries.append(entry)
        if self._sink is not None:
            self._sink(entry)
        return entry

    def for_entity(self, entity, entity_id):
        return [entry for entry in self.entries if entry.entity == entity and entry.entity_id == entity_id]

    def after(self, sequence):
        return [entry for entry in self.entries if entry.sequence > sequence]

    def export(self):
        return [{"sequence": e.sequence, "actor": e.actor, "action": e.action, "entity": e.entity, "entity_id": e.entity_id, "request_id": e.request_id, "detail": e.detail, "created_at": e.created_at.isoformat()} for e in self.entries]

    def load_rows(self, rows):
        """服务重启时按序号回填审计，不产生重复条目。"""
        for row in rows:
            entry = AuditEntry(
                int(row["sequence"]), row["actor"], row["action"], row["entity"],
                row["entity_id"], row["request_id"],
                dict(row["detail"]) if isinstance(row["detail"], dict) else json.loads(row["detail"]),
                parse_time(row["created_at"]))
            self.entries.append(entry)
            self._sequence = max(self._sequence, entry.sequence)
