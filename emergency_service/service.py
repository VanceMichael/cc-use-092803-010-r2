from .assignments import AssignmentBoard
from .audit import AuditLog
from .auth import Principal, require, can
from .events import EventRegister
from .models import Assignment, Event, SupplyLot, Team
from .plans import PlanBook
from .joint_plans import JointPlanBook
from .recovery import RecoveryJournal
from .store import StateStore
from .supplies import SupplyLedger
from .teams import TeamRegistry
from .geo import RiskMap
from .errors import PermissionDenied

class EmergencyPlatform:
    def __init__(self, db_path=":memory:"):
        self.audit = AuditLog()
        self.store = StateStore(db_path)
        self.journal = RecoveryJournal(self.audit)
        self.events = EventRegister(self.audit)
        self.teams = TeamRegistry(self.audit)
        self.supplies = SupplyLedger(self.audit)
        self.assignments = AssignmentBoard(self.events, self.teams, self.audit)
        self.risks = RiskMap(self.audit)
        self.plans = PlanBook(self.audit)
        self.joint_plans = JointPlanBook(
            self.store, self.events, self.teams, self.supplies, self.assignments, self.audit
        )

    def create_event(self, principal, event, request_id):
        require(principal, "plan", event.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        self.journal.record_pending(request_id, "create_event", {"event_id": event.event_id})
        result = self.events.create(event, request_id)
        payload = {"event_id": result.event_id, "status": result.status, "version": result.version}
        return self.store.save_request(request_id, payload)

    def assign(self, principal, assignment, request_id):
        event = self.events.get(assignment.event_id)
        require(principal, "assign", event.region)
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        result = self.assignments.plan(assignment, request_id)
        payload = {"assignment_id": result.assignment_id, "state": result.state}
        return self.store.save_request(request_id, payload)

    def acknowledge(self, principal, assignment_id, request_id):
        item = self.assignments.get(assignment_id)
        event = self.events.get(item.event_id)
        require(principal, "assign", event.region)
        result = self.assignments.acknowledge(assignment_id, request_id)
        return {"assignment_id": result.assignment_id, "state": result.state}

    def close(self, principal, event_id, request_id):
        event = self.events.get(event_id)
        require(principal, "close", event.region)
        result = self.events.change_status(event_id, "closed", request_id)
        return {"event_id": result.event_id, "status": result.status, "version": result.version}

    # ------------------------------------------------------ 跨县联合行动计划

    def _require_plan_permission(self, principal, plan_id):
        plan = self.joint_plans.get_plan(plan_id)
        require(principal, "plan", plan["region"])
        return plan

    def _idempotent(self, request_id, operation):
        cached = self.store.get_request(request_id)
        if cached is not None:
            return cached
        result = operation()
        return self.store.save_request(request_id, result)

    def create_joint_plan(self, principal, spec, request_id):
        # 跨县计划：指挥员必须对计划绑定的每一个灾害点所在县都有调度权
        regions = {self.events.get(event_id).region for event_id in spec.event_ids}
        for region in regions:
            require(principal, "plan", region)

        def operate():
            self.journal.record_pending(request_id, "create_joint_plan", {"plan_id": spec.plan_id})
            return self.joint_plans.create(spec, request_id)

        return self._idempotent(request_id, operate)

    def start_joint_plan(self, principal, plan_id, request_id):
        self._require_plan_permission(principal, plan_id)
        return self._idempotent(request_id, lambda: self.joint_plans.start_plan(plan_id, request_id))

    def pause_joint_plan(self, principal, plan_id, request_id):
        self._require_plan_permission(principal, plan_id)
        return self._idempotent(request_id, lambda: self.joint_plans.pause_plan(plan_id, request_id))

    def start_joint_step(self, principal, plan_id, step_id, request_id):
        self._require_plan_permission(principal, plan_id)
        return self._idempotent(request_id, lambda: self.joint_plans.start_step(plan_id, step_id, request_id))

    def confirm_joint_step(self, principal, plan_id, step_id, request_id):
        self._require_plan_permission(principal, plan_id)
        return self._idempotent(request_id, lambda: self.joint_plans.confirm_step(plan_id, step_id, request_id))

    def cancel_joint_step(self, principal, plan_id, step_id, request_id):
        self._require_plan_permission(principal, plan_id)
        return self._idempotent(request_id, lambda: self.joint_plans.cancel_step(plan_id, step_id, request_id))

    def reschedule_joint_plan(self, principal, plan_id, updated_steps, request_id):
        self._require_plan_permission(principal, plan_id)
        return self._idempotent(
            request_id, lambda: self.joint_plans.reschedule(plan_id, updated_steps, request_id)
        )

    def get_joint_plan(self, principal, plan_id):
        plan = self.joint_plans.get_plan(plan_id)
        if not (can(principal, "plan", plan["region"]) or can(principal, "read", plan["region"])):
            raise PermissionDenied(f"{principal.actor_id} 无权查看联合行动计划")
        return self.joint_plans.describe(plan_id)

    def recover_joint_plans(self, principal=None):
        """重启续办：从持久化层重载计划，给出每个未终结计划的续办点。"""
        self.joint_plans.reload()
        recovery = []
        for plan_id in sorted(self.joint_plans.plans):
            plan = self.joint_plans.get_plan(plan_id)
            if plan["state"] in {"draft", "completed"}:
                continue
            if principal is not None and not can(principal, "plan", plan["region"]) \
                    and not can(principal, "read", plan["region"]):
                continue
            snapshot = self.joint_plans.schedule(plan_id)
            recovery.append({
                "plan_id": plan_id,
                "state": plan["state"],
                "resume_from": snapshot["resume_from"],
                "in_flight_steps": [
                    item["step_id"] for item in snapshot["steps"] if item["state"] == "running"
                ],
                "execution_order": snapshot["execution_order"],
            })
        return recovery

    # -------------------------------------------------------------- 既有语义

    def recover(self):
        return self.journal.drain(lambda action, payload, request_id: None)

    def audit_for(self, principal, entity, entity_id):
        require(principal, "read_audit", "global")
        return self.audit.for_entity(entity, entity_id)
