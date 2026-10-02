"""跨县联合行动计划。

把多个灾害点（事件）、派单（队伍）与物资占用锁定到同一张持久化的依赖图中，
按 依赖 → 资源冲突 → 优先级 计算可执行顺序，支持局部重排、暂停与重启续办。

优先级约定：priority 数值越小越优先，默认 100。
"""
from dataclasses import dataclass, field

from .clock import utc_now
from .errors import Conflict, InvalidTransition, NotFound
from .models import JointPlan, PlanStep, SupplyUse

TERMINAL_STATES = {"completed", "cancelled"}


@dataclass
class StepSpec:
    step_id: str
    event_id: str
    assignment_id: str
    priority: int = 100
    depends_on: list[str] = field(default_factory=list)
    supplies: list[SupplyUse] = field(default_factory=list)


class JointPlanBook:
    def __init__(self, audit, events, teams, supplies, assignments):
        self.audit = audit
        self.events = events
        self.teams = teams
        self.supplies = supplies
        self.assignments = assignments
        self.plans: dict[str, JointPlan] = {}
        self.steps: dict[str, dict[str, PlanStep]] = {}

    # ------------------------------------------------------------------ 创建
    def create_plan(self, plan_id, region, specs, request_id):
        if plan_id in self.plans:
            raise Conflict("联合行动计划已存在")
        if not specs:
            raise ValueError("联合行动计划至少包含一个步骤")
        step_ids = [spec.step_id for spec in specs]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("步骤编号不能重复")
        known = set(step_ids)

        steps: dict[str, PlanStep] = {}
        planned_reservations: list[tuple[str, int]] = []
        bound: list[str] = []
        try:
            for spec in specs:
                unknown = [dep for dep in spec.depends_on if dep not in known]
                if unknown:
                    raise ValueError(f"步骤 {spec.step_id} 依赖了不存在的步骤: {','.join(sorted(unknown))}")
                assignment = self.assignments.get(spec.assignment_id)
                event = self.events.get(spec.event_id)
                if assignment.event_id != spec.event_id:
                    raise ValueError(f"步骤 {spec.step_id} 的派单与灾害点不一致")
                if assignment.state in TERMINAL_STATES:
                    raise InvalidTransition(f"派单 {assignment.assignment_id} 已终态，不能锁定")
                for use in spec.supplies:
                    if use.quantity <= 0:
                        raise ValueError("物资占用量必须为正")
                # 原子预留：累计本计划内的占用后一次性校验可用性
                for use in spec.supplies:
                    lot = self.supplies.get(use.lot_id)
                    if lot.frozen:
                        raise InvalidTransition(f"物资批次 {use.lot_id} 已冻结，不能占用")
                    planned = sum(q for lid, q in planned_reservations if lid == use.lot_id)
                    if lot.quantity - lot.reserved - planned < use.quantity:
                        raise InvalidTransition(f"物资批次 {use.lot_id} 可用量不足")
                for use in spec.supplies:
                    self.supplies.reserve(use.lot_id, use.quantity, request_id)
                    planned_reservations.append((use.lot_id, use.quantity))
                self.assignments.bind(spec.assignment_id, plan_id, spec.step_id, request_id)
                bound.append(spec.assignment_id)
                steps[spec.step_id] = PlanStep(
                    step_id=spec.step_id,
                    event_id=spec.event_id,
                    assignment_id=spec.assignment_id,
                    team_id=assignment.team_id,
                    priority=spec.priority,
                    dependencies=set(spec.depends_on),
                    supplies=list(spec.supplies),
                    updated_at=utc_now(),
                )
        except Exception:
            # 回滚本请求已做的预留与绑定，保持原有物资账/派单语义
            for lot_id, amount in planned_reservations:
                self.supplies.release(lot_id, amount, request_id + "-rollback")
            for assignment_id in bound:
                self.assignments.unbind(assignment_id, request_id + "-rollback")
            raise

        graph = {sid: step.dependencies for sid, step in steps.items()}
        self._validate_graph(graph)

        now = utc_now()
        self.plans[plan_id] = JointPlan(plan_id, region, "draft", now, now)
        self.steps[plan_id] = steps
        self.audit.append("create_joint_plan", "joint_plan", plan_id, request_id,
                          {"region": region, "steps": step_ids, "events": sorted({s.event_id for s in steps.values()})})
        return self.view(plan_id)

    def _validate_graph(self, graph):
        visiting, visited = set(), set()

        def visit(node):
            if node in visiting:
                raise ValueError("行动依赖存在环")
            if node in visited:
                return
            visiting.add(node)
            for parent in graph.get(node, set()):
                visit(parent)
            visiting.discard(node)
            visited.add(node)

        for node in graph:
            visit(node)

    def get(self, plan_id):
        if plan_id not in self.plans:
            raise NotFound("联合行动计划不存在")
        return self.plans[plan_id]

    # ------------------------------------------------------------------ 调度
    def _running_team_holders(self, exclude_plan):
        """其他计划中正在占用队伍的步骤：跨计划资源冲突。"""
        holders = {}
        for other_id, steps in self.steps.items():
            if other_id == exclude_plan:
                continue
            for step in steps.values():
                if step.state == "running":
                    holders[step.team_id] = (other_id, step.step_id)
        return holders

    def schedule(self, plan_id):
        plan = self.get(plan_id)
        steps = self.steps[plan_id]
        active = {sid: s for sid, s in steps.items() if s.state not in TERMINAL_STATES}

        dep_ready, blocked = [], []
        for sid, step in active.items():
            waiting_deps = []
            cancelled_deps = []
            for dep in sorted(step.dependencies):
                dep_state = steps[dep].state
                if dep_state == "completed":
                    continue
                if dep_state == "cancelled":
                    cancelled_deps.append(dep)
                else:
                    waiting_deps.append(f"{dep}({dep_state})")
            if cancelled_deps:
                blocked.append((step, [f"依赖步骤 {','.join(cancelled_deps)} 已取消，需要重排依赖"]))
            elif waiting_deps:
                blocked.append((step, [f"等待前序步骤 {','.join(waiting_deps)} 完成"]))
            else:
                dep_ready.append(step)

        # 依赖已满足的步骤，按优先级（小者优先）、编号稳定排序后争抢队伍
        ordered = sorted(dep_ready, key=lambda s: (s.priority, s.step_id))
        external = self._running_team_holders(plan_id)
        local_holders: dict[str, str] = {}
        ready_ids, entries = set(), {}

        for step in ordered:
            reasons = []
            if step.team_id in external:
                other_plan, holder = external[step.team_id]
                reasons.append(f"队伍 {step.team_id} 正被计划 {other_plan} 的步骤 {holder} 占用")
            elif step.team_id in local_holders:
                reasons.append(f"队伍 {step.team_id} 被同计划优先级更高的步骤 {local_holders[step.team_id]} 占用")
            frozen_lots = [use.lot_id for use in step.supplies if self.supplies.get(use.lot_id).frozen]
            if frozen_lots:
                reasons.append(f"物资批次 {','.join(frozen_lots)} 已冻结")
            if plan.state == "paused":
                reasons.append("联合行动计划已暂停")
            if reasons:
                blocked.append((step, reasons))
            else:
                local_holders[step.team_id] = step.step_id
                ready_ids.add(step.step_id)
                entries[step.step_id] = reasons

        for step, reasons in blocked:
            entries[step.step_id] = reasons

        # running 步骤继续占位，不参与重新排队
        for step in active.values():
            if step.state == "running":
                entries.setdefault(step.step_id, [])

        return plan, steps, ready_ids, entries

    def _resource_view(self, step):
        return {
            "team_id": step.team_id,
            "supplies": [
                {"lot_id": use.lot_id, "item": self.supplies.get(use.lot_id).item,
                 "quantity": use.quantity, "reserved": self.supplies.get(use.lot_id).reserved}
                for use in step.supplies
            ],
        }

    def view(self, plan_id):
        plan, steps, ready_ids, reasons = self.schedule(plan_id)
        running = sorted(sid for sid, s in steps.items() if s.state == "running")
        # 可执行顺序：执行中步骤在前，随后是本轮可派的就绪队列
        order = running + sorted(ready_ids, key=lambda sid: (steps[sid].priority, sid))

        step_views = []
        for sid in sorted(steps, key=lambda x: (steps[x].priority, x)):
            step = steps[sid]
            waiting = reasons.get(sid, [])
            if step.state == "completed":
                status, reason = "completed", "已完成"
            elif step.state == "cancelled":
                status, reason = "cancelled", "已取消"
            elif step.state == "running":
                status, reason = "running", "执行中，重启或暂停恢复后从本步骤续办"
            elif sid in ready_ids:
                status, reason = "ready", "可执行"
            else:
                status = "blocked"
                reason = "；".join(waiting) if waiting else "等待"
            step_views.append({
                "step_id": sid,
                "event_id": step.event_id,
                "region": self.events.get(step.event_id).region,
                "assignment_id": step.assignment_id,
                "priority": step.priority,
                "depends_on": sorted(step.dependencies),
                "state": status,
                "resources": self._resource_view(step),
                "blocked_by": waiting,
                "waiting_reason": reason,
                "resume_from": step.resume_from or sid,
                "attempts": step.attempts,
                "order": order.index(sid) if sid in order else None,
            })

        return {
            "plan_id": plan.plan_id,
            "region": plan.region,
            "state": plan.state,
            "steps": step_views,
            "executable_order": order,
            "resume_from": running or [sid for sid in order if steps[sid].state != "running"][:1],
            "reschedulable": self.downstream_of_cancelled(plan_id),
        }

    # ------------------------------------------------------------------ 状态流转
    def start(self, plan_id, request_id):
        plan = self.get(plan_id)
        if plan.state not in {"draft", "paused"}:
            raise InvalidTransition("联合行动计划只能在草稿或暂停状态启动")
        plan.state = "running"
        plan.updated_at = utc_now()
        self.audit.append("start_joint_plan", "joint_plan", plan_id, request_id, {})
        return self.view(plan_id)

    def pause(self, plan_id, request_id):
        plan = self.get(plan_id)
        if plan.state != "running":
            raise InvalidTransition("只有执行中的联合行动计划可暂停")
        plan.state = "paused"
        plan.updated_at = utc_now()
        self.audit.append("pause_joint_plan", "joint_plan", plan_id, request_id,
                          {"resume_from": [s.step_id for s in self.steps[plan_id].values() if s.state == "running"]})
        return self.view(plan_id)

    def _require_ready(self, plan_id, step_id):
        plan, steps, ready_ids, _ = self.schedule(plan_id)
        if plan.state != "running":
            raise InvalidTransition("联合行动计划未在执行中")
        step = steps.get(step_id)
        if step is None:
            raise NotFound("步骤不存在")
        return step, step_id in ready_ids

    def confirm_step(self, plan_id, step_id, request_id):
        """现场确认接单。重复确认保持幂等：不重复推进派单、不累计次数。"""
        step, is_ready = self._require_ready(plan_id, step_id)
        if step.state == "running":
            return {**self.view(plan_id), "idempotent": True,
                    "notice": f"步骤 {step_id} 已在执行中，确认请求 {request_id} 按重复确认处理"}
        if step.state == "completed":
            return {**self.view(plan_id), "idempotent": True, "notice": f"步骤 {step_id} 已完成"}
        if not is_ready:
            raise InvalidTransition("步骤当前不满足执行条件")
        step.state = "running"
        step.attempts += 1
        step.confirmed_request = request_id
        step.resume_from = step_id
        step.updated_at = utc_now()
        self.assignments.acknowledge(step.assignment_id, request_id, locked_ok=True)
        self.audit.append("confirm_plan_step", "joint_plan", plan_id, request_id, {"step_id": step_id})
        return self.view(plan_id)

    def complete_step(self, plan_id, step_id, request_id):
        step, _ = self._require_ready(plan_id, step_id)
        if step.state == "completed":
            return {**self.view(plan_id), "idempotent": True, "notice": f"步骤 {step_id} 已完成"}
        if step.state != "running":
            raise InvalidTransition("步骤尚未确认执行，不能完成")
        step.state = "completed"
        step.updated_at = utc_now()
        self.assignments.complete(step.assignment_id, request_id, locked_ok=True)
        for use in step.supplies:
            self.supplies.consume_reserved(use.lot_id, use.quantity, request_id)
        self.audit.append("complete_plan_step", "joint_plan", plan_id, request_id,
                          {"step_id": step_id, "consumed": [{"lot_id": u.lot_id, "quantity": u.quantity} for u in step.supplies]})
        self._maybe_complete(plan_id, request_id)
        return self.view(plan_id)

    def _maybe_complete(self, plan_id, request_id):
        plan = self.get(plan_id)
        if plan.state != "running":
            return
        if not all(s.state in TERMINAL_STATES for s in self.steps[plan_id].values()):
            return
        plan.state = "completed" if any(s.state == "completed" for s in self.steps[plan_id].values()) else "cancelled"
        plan.updated_at = utc_now()
        self.audit.append("close_joint_plan", "joint_plan", plan_id, request_id, {"state": plan.state})

    def downstream_of_cancelled(self, plan_id):
        steps = self.steps[plan_id]
        result = {}
        for sid, step in steps.items():
            if step.state != "cancelled":
                continue
            reachable = set()
            frontier = [sid]
            while frontier:
                current = frontier.pop()
                for other, other_step in steps.items():
                    if other not in reachable and current in other_step.dependencies and other_step.state not in TERMINAL_STATES:
                        reachable.add(other)
                        frontier.append(other)
            if reachable:
                result[sid] = sorted(reachable)
        return result

    def cancel_step(self, plan_id, step_id, request_id):
        """取消未完成步骤：释放队伍能力与物资占用，已完成步骤不受影响。"""
        plan = self.get(plan_id)
        step = self.steps[plan_id].get(step_id)
        if step is None:
            raise NotFound("步骤不存在")
        if step.state == "completed":
            raise InvalidTransition("已完成步骤不能取消")
        if step.state == "cancelled":
            return {**self.view(plan_id), "idempotent": True, "notice": f"步骤 {step_id} 已取消"}
        step.state = "cancelled"
        step.updated_at = utc_now()
        released_supplies = [{"lot_id": u.lot_id, "quantity": u.quantity} for u in step.supplies]
        for use in step.supplies:
            self.supplies.release(use.lot_id, use.quantity, request_id)
        self.assignments.unbind(step.assignment_id, request_id)
        self.assignments.cancel(step.assignment_id, request_id)
        self.audit.append("cancel_plan_step", "joint_plan", plan_id, request_id,
                          {"step_id": step_id, "released_supplies": released_supplies,
                           "downstream": self.downstream_of_cancelled(plan_id)})
        self._maybe_complete(plan_id, request_id)
        return self.view(plan_id)

    # ------------------------------------------------------------------ 局部重排
    def reschedule(self, plan_id, updates, new_specs, request_id):
        """只调整未开始（pending）的步骤；执行中与已完成步骤保持不动。"""
        plan = self.get(plan_id)
        steps = self.steps[plan_id]
        new_specs = new_specs or []
        touched = []
        originals = {}

        for step_id, change in (updates or {}).items():
            step = steps.get(step_id)
            if step is None:
                raise NotFound(f"步骤 {step_id} 不存在")
            if step.state == "completed":
                raise InvalidTransition(f"已完成步骤 {step_id} 不参与重排")
            if step.state == "cancelled":
                raise InvalidTransition(f"已取消步骤 {step_id} 不参与重排")
            if step.state == "running":
                raise InvalidTransition(f"执行中步骤 {step_id} 需先暂停计划才能重排")
            originals[step_id] = (step.priority, set(step.dependencies))
            if "priority" in change:
                step.priority = int(change["priority"])
            if "depends_on" in change:
                deps = set(change["depends_on"])
                unknown = [dep for dep in deps if dep not in steps and dep not in {s.step_id for s in new_specs}]
                if unknown:
                    raise ValueError(f"依赖了不存在的步骤: {','.join(sorted(unknown))}")
                if any(steps[dep].state == "cancelled" for dep in deps if dep in steps):
                    raise InvalidTransition("不能把依赖指向已取消步骤")
                step.dependencies = deps
            touched.append(step_id)

        reservations, bound, added = [], [], []
        try:
            for spec in new_specs:
                if spec.step_id in steps:
                    raise Conflict(f"步骤 {spec.step_id} 已存在")
                assignment = self.assignments.get(spec.assignment_id)
                if assignment.event_id != spec.event_id:
                    raise ValueError(f"步骤 {spec.step_id} 的派单与灾害点不一致")
                if assignment.state in TERMINAL_STATES:
                    raise InvalidTransition(f"派单 {assignment.assignment_id} 已终态，不能锁定")
                if assignment.plan_id is not None:
                    raise Conflict(f"派单 {assignment.assignment_id} 已被其他步骤锁定")
                for use in spec.supplies:
                    lot = self.supplies.get(use.lot_id)
                    if lot.frozen:
                        raise InvalidTransition(f"物资批次 {use.lot_id} 已冻结")
                    planned = sum(q for lid, q in reservations if lid == use.lot_id)
                    if lot.quantity - lot.reserved - planned < use.quantity:
                        raise InvalidTransition(f"物资批次 {use.lot_id} 可用量不足")
                for use in spec.supplies:
                    self.supplies.reserve(use.lot_id, use.quantity, request_id)
                    reservations.append((use.lot_id, use.quantity))
                self.assignments.bind(spec.assignment_id, plan_id, spec.step_id, request_id)
                bound.append(spec.assignment_id)
                steps[spec.step_id] = PlanStep(
                    step_id=spec.step_id, event_id=spec.event_id,
                    assignment_id=spec.assignment_id, team_id=assignment.team_id,
                    priority=spec.priority, dependencies=set(spec.depends_on),
                    supplies=list(spec.supplies), updated_at=utc_now())
                added.append(spec.step_id)
                touched.append(spec.step_id)
        except Exception:
            for lot_id, amount in reservations:
                self.supplies.release(lot_id, amount, request_id + "-rollback")
            for assignment_id in bound:
                self.assignments.unbind(assignment_id, request_id + "-rollback")
            for step_id in added:
                steps.pop(step_id, None)
            for step_id, (priority, deps) in originals.items():
                steps[step_id].priority = priority
                steps[step_id].dependencies = deps
            raise

        self._validate_graph({sid: s.dependencies for sid, s in steps.items()})
        plan.updated_at = utc_now()
        self.audit.append("reschedule_joint_plan", "joint_plan", plan_id, request_id, {"touched": touched})
        return self.view(plan_id)

    # ------------------------------------------------------------------ 重启装载
    def restore(self, plan, steps):
        """服务重启后用仓储快照重建内存状态，不产生新审计。"""
        self.plans[plan.plan_id] = plan
        self.steps[plan.plan_id] = {s.step_id: s for s in steps}
