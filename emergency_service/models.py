from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

@dataclass
class Event:
    event_id: str
    region: str
    kind: str
    severity: int
    occurred_at: datetime
    source: str
    status: str = "open"
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class Team:
    team_id: str
    region: str
    skills: set[str]
    capacity: int
    active: bool = True

@dataclass
class SupplyLot:
    lot_id: str
    item: str
    quantity: int
    reserved: int = 0
    frozen: bool = False

@dataclass
class Assignment:
    assignment_id: str
    event_id: str
    team_id: str
    state: str = "planned"
    quantity: int = 0
    updated_at: datetime | None = None
    # 被联合行动计划步骤锁定时记录归属；空闲派单该字段为 None
    plan_id: str | None = None
    step_id: str | None = None

@dataclass
class AuditEntry:
    sequence: int
    actor: str
    action: str
    entity: str
    entity_id: str
    request_id: str
    detail: dict[str, Any]
    created_at: datetime

@dataclass
class SupplyUse:
    """步骤对单个物资批次的占用量。"""
    lot_id: str
    quantity: int

@dataclass
class PlanStep:
    """联合行动计划中的一个步骤，锁定绑定灾害点、派单与物资占用。"""
    step_id: str
    event_id: str
    assignment_id: str
    team_id: str
    priority: int = 100
    dependencies: set[str] = field(default_factory=set)
    supplies: list[SupplyUse] = field(default_factory=list)
    state: str = "pending"            # pending|ready|running|completed|cancelled
    blocked_by: list[str] = field(default_factory=list)
    waiting_reason: str = ""
    resume_from: str = ""
    attempts: int = 0
    confirmed_request: str | None = None
    updated_at: datetime | None = None

@dataclass
class JointPlan:
    plan_id: str
    region: str
    state: str = "draft"              # draft|running|paused|completed|cancelled
    created_at: datetime | None = None
    updated_at: datetime | None = None
