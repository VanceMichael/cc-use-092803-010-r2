import json

from .assignments import AssignmentBoard
from .audit import AuditLog
from .auth import Principal, require
from .clock import parse_time
from .events import EventRegister
from .joint import JointPlanBook, StepSpec
from .models import Assignment, Event, JointPlan, PlanStep, SupplyLot, SupplyUse, Team
from .plans import PlanBook
from .recovery import RecoveryJournal
from .store import StateStore
from .supplies import SupplyLedger
from .teams import TeamRegistry
from .geo import RiskMap


class EmergencyPlatform:
    def __init__(self, db_path=":memory:"):
        self.audit = AuditLog()
        self.store = StateStore(db_path)
        self.audit.set_sink(self.store.insert_audit)
        self.journal = RecoveryJournal(self.audit)
        self.events = EventRegister(self.audit)
        self.teams = TeamRegistry(self.audit)
        self.supplies = SupplyLedger(self.audit)
        self.assignments = AssignmentBoard(self.events, self.teams, self.audit)
        self.risks = RiskMap(self.audit)
        self.plans = PlanBook(self.audit)
        self.joint_plans = JointPlanBook(self.audit, self.events, self.teams, self.supplies, self.assignments)
        self._restore()

    # ------------------------------------------------------------------ 持久化
    def _snapshot_state(self):
        return {
            "events": list(self.events.events.values()),
            "teams": list(self.teams.teams.values()),
            "lots": list(self.supplies.lots.values()),
            "assignments": list(self.assignments.items.values()),
            "plans": [(plan, list(steps.values())) for plan, steps in
                      ((self.joint_plans.plans[pid], self.joint_plans.steps[pid])
                       for pid in self.joint_plans.plans)],
        }

    def _persist(self):
        self.store.snapshot(self._snapshot_state())

    def _restore(self):
        """从 SQLite 重建内存领域状态，保证服务重启后续办。"""
        self.audit.load_rows(self.store.load_audit())
        data = self.store.load_snapshot()
        for row in data["events"]:
            event = Event(row["event_id"], row["region"], row["kind"], row["severity"],
                          parse_time(row["occurred_at"]), row["source"], row["status"],
                          row["version"], json.loads(row["metadata"]))
            self.events.events[event.event_id] = event
        for row in data["teams"]:
            team = Team(row["team_id"], row["region"], set(json.loads(row["skills"])),
                        row["capacity"], bool(row["active"]))
            self.teams.teams[team.team_id] = team
        for row in data["lots"]:
            lot = SupplyLot(row["lot_id"], row["item"], row["quantity"], row["reserved"], bool(row["frozen"]))
            self.supplies.lots[lot.lot_id] = lot
        for row in data["assignments"]:
            item = Assignment(row["assignment_id"], row["event_id"], row["team_id"], row["state"],
                              row["quantity"], parse_time(row["updated_at"]) if row["updated_at"] else None,
                              row["plan_id"], row["step_id"])
            self.assignments.items[item.assignment_id] = item
        deps = {}
        for row in data["deps"]:
            deps.setdefault((row["plan_id"], row["step_id"]), set()).add(row["depends_on"])
        uses = {}
        for row in data["supplies"]:
            uses.setdefault((row["plan_id"], row["step_id"]), []).append(SupplyUse(row["lot_id"], row["quantity"]))
        steps_by_plan = {}
        for row in data["steps"]:
            step = PlanStep(
                step_id=row["step_id"], event_id=row["event_id"],
                assignment_id=row["assignment_id"], team_id=row["team_id"],
                priority=row["priority"],
                dependencies=deps.get((row["plan_id"], row["step_id"]), set()),
                supplies=uses.get((row["plan_id"], row["step_id"]), []),
                state=row["state"], blocked_by=json.loads(row["blocked_by"]),
                waiting_reason=row["waiting_reason"], resume_from=row["resume_from"],
                attempts=row["attempts"], confirmed_request=row["confirmed_request"],
                updated_at=parse_time(row["updated_at"]) if row["updated_at"] else None)
            steps_by_plan.setdefault(row["plan_id"], []).append(step)
        for row in data["plans"]:
            plan = JointPlan(row["plan_id"], row["region"], row["state"],
                             parse_time(row["created_at"]) if row["created_at"] else None,
                             parse_time(row["updated_at"]) if row["updated_at"] else None)
            self.joint_plans.restore(plan, steps_by_plan.get(plan.plan_id, []))

    # ------------------------------------------------------------------ 基础语义
    def create_event(self, principal, event, request_id):
        require(principal, "plan", event.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            self.journal.record_pending(request_id, "create_event", {"event_id": event.event_id})
            result = self.events.create(event, request_id)
            payload = {"event_id": result.event_id, "status": result.status, "version": result.version}
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, payload)

    def assign(self, principal, assignment, request_id):
        event = self.events.get(assignment.event_id)
        require(principal, "assign", event.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            result = self.assignments.plan(assignment, request_id)
            payload = {"assignment_id": result.assignment_id, "state": result.state}
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, payload)

    def acknowledge(self, principal, assignment_id, request_id):
        item = self.assignments.get(assignment_id)
        event = self.events.get(item.event_id)
        require(principal, "assign", event.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            result = self.assignments.acknowledge(assignment_id, request_id)
            payload = {"assignment_id": result.assignment_id, "state": result.state}
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, payload)

    def close(self, principal, event_id, request_id):
        event = self.events.get(event_id)
        require(principal, "close", event.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            result = self.events.change_status(event_id, "closed", request_id)
            payload = {"event_id": result.event_id, "status": result.status, "version": result.version}
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, payload)

    def recover(self):
        return self.journal.drain(lambda action, payload, request_id: None)

    def audit_for(self, principal, entity, entity_id):
        require(principal, "read_audit", "global")
        return self.audit.for_entity(entity, entity_id)

    # ------------------------------------------------------------------ 联合行动计划
    def _require_joint_commander(self, principal, region):
        require(principal, "joint_plan", region)

    def _specs(self, raw_steps):
        specs = []
        for raw in raw_steps:
            specs.append(StepSpec(
                step_id=str(raw["step_id"]),
                event_id=str(raw["event_id"]),
                assignment_id=str(raw["assignment_id"]),
                priority=int(raw.get("priority", 100)),
                depends_on=[str(dep) for dep in raw.get("depends_on", [])],
                supplies=[SupplyUse(str(s["lot_id"]), int(s["quantity"])) for s in raw.get("supplies", [])],
            ))
        return specs

    def create_joint_plan(self, principal, plan_id, region, raw_steps, request_id):
        self._require_joint_commander(principal, region)
        # 跨县联合：指挥员须覆盖计划涉及的全部灾害点所在县
        for spec in self._specs(raw_steps):
            event = self.events.get(spec.event_id)
            require(principal, "joint_plan", event.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            view = self.joint_plans.create_plan(plan_id, region, self._specs(raw_steps), request_id)
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, view)

    def start_joint_plan(self, principal, plan_id, request_id):
        plan = self.joint_plans.get(plan_id)
        self._require_joint_commander(principal, plan.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            view = self.joint_plans.start(plan_id, request_id)
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, view)

    def pause_joint_plan(self, principal, plan_id, request_id):
        plan = self.joint_plans.get(plan_id)
        self._require_joint_commander(principal, plan.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            view = self.joint_plans.pause(plan_id, request_id)
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, view)

    def confirm_step(self, principal, plan_id, step_id, request_id):
        plan = self.joint_plans.get(plan_id)
        self._require_joint_commander(principal, plan.region)
        # 重复确认（同一 request_id）直接回放首次结果，保持幂等
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            view = self.joint_plans.confirm_step(plan_id, step_id, request_id)
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, view)

    def complete_step(self, principal, plan_id, step_id, request_id):
        plan = self.joint_plans.get(plan_id)
        self._require_joint_commander(principal, plan.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            view = self.joint_plans.complete_step(plan_id, step_id, request_id)
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, view)

    def cancel_step(self, principal, plan_id, step_id, request_id):
        plan = self.joint_plans.get(plan_id)
        self._require_joint_commander(principal, plan.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            view = self.joint_plans.cancel_step(plan_id, step_id, request_id)
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, view)

    def reschedule(self, principal, plan_id, updates, new_steps, request_id):
        plan = self.joint_plans.get(plan_id)
        self._require_joint_commander(principal, plan.region)
        for spec in self._specs(new_steps or []):
            event = self.events.get(spec.event_id)
            require(principal, "joint_plan", event.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.audit.actor = principal.actor_id
        try:
            view = self.joint_plans.reschedule(plan_id, updates or {}, self._specs(new_steps or []), request_id)
            self._persist()
        finally:
            self.audit.actor = "system"
        return self.store.save_request(request_id, view)

    def joint_plan_view(self, principal, plan_id):
        plan = self.joint_plans.get(plan_id)
        require(principal, "joint_plan", plan.region)
        return self.joint_plans.view(plan_id)
