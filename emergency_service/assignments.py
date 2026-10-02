from .clock import utc_now
from .errors import Conflict, InvalidTransition, NotFound

class AssignmentBoard:
    def __init__(self, events, teams, audit):
        self.events = events
        self.teams = teams
        self.audit = audit
        self.items = {}

    def plan(self, assignment, request_id):
        if assignment.assignment_id in self.items:
            old = self.items[assignment.assignment_id]
            if old.event_id == assignment.event_id and old.team_id == assignment.team_id:
                return old
            raise Conflict("派单编号冲突")
        event = self.events.get(assignment.event_id)
        if event.status == "closed":
            raise InvalidTransition("关闭事件不能派单")
        self.teams.consume(assignment.team_id, max(1, assignment.quantity), request_id)
        self.items[assignment.assignment_id] = assignment
        self.audit.append("plan_assignment", "assignment", assignment.assignment_id, request_id, {"event_id": assignment.event_id})
        return assignment

    def get(self, assignment_id):
        if assignment_id not in self.items:
            raise NotFound("派单不存在")
        return self.items[assignment_id]

    def acknowledge(self, assignment_id, request_id, locked_ok=False):
        item = self.get(assignment_id)
        if item.plan_id is not None and not locked_ok:
            raise InvalidTransition("派单已被联合行动计划步骤锁定，须在计划内确认")
        if item.state != "planned":
            raise InvalidTransition("派单不是待确认状态")
        item.state = "acknowledged"
        item.updated_at = utc_now()
        self.audit.append("ack_assignment", "assignment", assignment_id, request_id, {})
        return item

    def complete(self, assignment_id, request_id, locked_ok=False):
        item = self.get(assignment_id)
        if item.plan_id is not None and not locked_ok:
            raise InvalidTransition("派单已被联合行动计划步骤锁定，须在计划内完成")
        if item.state != "acknowledged":
            raise InvalidTransition("派单尚未确认")
        item.state = "completed"
        item.updated_at = utc_now()
        self.audit.append("complete_assignment", "assignment", assignment_id, request_id, {})
        return item

    def bind(self, assignment_id, plan_id, step_id, request_id):
        item = self.get(assignment_id)
        if item.plan_id is not None:
            raise Conflict("派单已被其他行动计划步骤锁定")
        if item.state in {"completed", "cancelled"}:
            raise InvalidTransition("终态派单不能锁定")
        item.plan_id = plan_id
        item.step_id = step_id
        self.audit.append("bind_assignment", "assignment", assignment_id, request_id, {"plan_id": plan_id, "step_id": step_id})
        return item

    def unbind(self, assignment_id, request_id):
        item = self.get(assignment_id)
        item.plan_id = None
        item.step_id = None
        self.audit.append("unbind_assignment", "assignment", assignment_id, request_id, {})
        return item

    def cancel(self, assignment_id, request_id):
        item = self.get(assignment_id)
        if item.plan_id is not None:
            raise InvalidTransition("派单已被联合行动计划步骤锁定，须取消对应计划步骤")
        if item.state in {"completed", "cancelled"}:
            raise InvalidTransition("派单不能取消")
        item.state = "cancelled"
        self.teams.restore(item.team_id, max(1, item.quantity), request_id)
        self.audit.append("cancel_assignment", "assignment", assignment_id, request_id, {})
        return item
