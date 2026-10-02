"""跨县联合行动计划。

把多个灾害点、派单与物资占用组织成一个持久化的联合行动计划：
- 步骤通过 dependencies 声明先后顺序；
- 调度器按 依赖 / 资源冲突 / 优先级 计算可执行顺序；
- 已完成步骤永不回滚，支持局部重排、暂停、重启续办；
- 所有状态写入 StateStore（SQLite），进程重启后可恢复；
- 变更以 request_id 去重，重复确认保持幂等。

步骤状态机：
    pending ──start──▶ running ──complete──▶ completed
                        │                     ▲
                        └──── (计划暂停，保持) ◀┘
    pending/running ──cancel──▶ cancelled（running 取消释放资源）
"""
import json
from datetime import datetime

from .clock import utc_now
from .errors import Conflict, InvalidTransition, NotFound
from .models import JointPlan, JointPlanStep

_TERMINAL_STATES = {"completed", "cancelled"}
_RUNNABLE_STATES = {"pending", "running"}


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else (value or "")


class JointPlanBook:
    def __init__(self, store, events, teams, supplies, assignments, audit):
        self.store = store
        self.events = events
        self.teams = teams
        self.supplies = supplies
        self.assignments = assignments
        self.audit = audit
        self._plans: dict[str, dict] = {}
        self._load_all()

    # ------------------------------------------------------------------ 装载

    def _load_all(self):
        for row in self.store.query("SELECT * FROM joint_plans"):
            plan = {
                "plan_id": row["plan_id"],
                "region": row["region"],
                "state": row["state"],
                "event_ids": json.loads(row["event_ids"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            plan["steps"] = self._load_steps(plan["plan_id"])
            self._plans[plan["plan_id"]] = plan

    def _load_steps(self, plan_id):
        steps = {}
        for row in self.store.query("SELECT * FROM joint_steps WHERE plan_id=?", (plan_id,)):
            steps[row["step_id"]] = {
                "step_id": row["step_id"],
                "title": row["title"],
                "priority": row["priority"],
                "event_id": row["event_id"],
                "assignment_id": row["assignment_id"],
                "team_id": row["team_id"],
                "crew": row["crew"],
                "dependencies": set(json.loads(row["dependencies"])),
                "state": row["state"],
                "hold_reason": row["hold_reason"],
                "blocked_by": json.loads(row["blocked_by"]),
                "resources": json.loads(row["resources"]),
                "updated_at": row["updated_at"],
                "lots": {},
            }
        for row in self.store.query("SELECT * FROM joint_step_lots WHERE plan_id=?", (plan_id,)):
            steps[row["step_id"]]["lots"][row["lot_id"]] = row["quantity"]
        return steps

    def reload(self):
        """从持久化层重新装载（例如进程重启或外部写入后）。"""
        self._plans.clear()
        self._load_all()

    def _get_plan(self, plan_id):
        plan = self._plans.get(plan_id)
        if plan is None:
            raise NotFound("联合行动计划不存在")
        return plan

    def get_plan(self, plan_id):
        """供服务层鉴权使用：返回计划内部记录，不存在则抛 NotFound。"""
        return self._get_plan(plan_id)

    @property
    def plans(self):
        return self._plans

    # ------------------------------------------------------------------ 建图

    def create(self, spec: JointPlan, request_id):
        if spec.plan_id in self._plans:
            raise Conflict("联合行动计划已存在")
        if not spec.event_ids:
            raise ValueError("联合行动计划至少要绑定一个灾害点")
        regions = set()
        for event_id in spec.event_ids:
            regions.add(self.events.get(event_id).region)
        if spec.region not in regions:
            raise ValueError("计划区域必须属于其绑定的灾害点")

        if not spec.steps:
            raise ValueError("联合行动计划至少包含一个步骤")
        steps: dict[str, dict] = {}
        for item in spec.steps:
            if item.step_id in steps:
                raise Conflict(f"步骤编号重复: {item.step_id}")
            if item.priority < 0:
                raise ValueError("优先级不能为负")
            if item.event_id is not None and item.event_id not in spec.event_ids:
                raise ValueError(f"步骤 {item.step_id} 绑定了计划外灾害点")
            self._validate_resource_links(item)
            steps[item.step_id] = self._step_from_spec(item)

        graph = {sid: step["dependencies"] for sid, step in steps.items()}
        self._validate_graph(graph)

        now = _iso(utc_now())
        self.store.execute(
            "INSERT INTO joint_plans(plan_id,region,state,event_ids,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (spec.plan_id, spec.region, "draft", json.dumps(spec.event_ids, ensure_ascii=False), now, now),
        )
        for step in steps.values():
            self._insert_step_row(spec.plan_id, step)
        plan = {
            "plan_id": spec.plan_id,
            "region": spec.region,
            "state": "draft",
            "event_ids": list(spec.event_ids),
            "created_at": now,
            "updated_at": now,
            "steps": steps,
        }
        self._plans[spec.plan_id] = plan
        self.audit.append("create_joint_plan", "joint_plan", spec.plan_id, request_id,
                          {"events": spec.event_ids, "steps": sorted(steps)})
        return self.describe(spec.plan_id)

    def _validate_resource_links(self, item: JointPlanStep):
        crew = max(1, item.crew)
        if item.assignment_id is not None:
            linked = self.assignments.get(item.assignment_id)
            if linked.event_id != item.event_id:
                raise ValueError(f"步骤 {item.step_id} 的派单与灾害点不一致")
            if linked.state in {"completed", "cancelled"}:
                raise InvalidTransition(f"派单 {item.assignment_id} 已终结，不能再绑定")
            if item.team_id is None:
                item.team_id = linked.team_id
            elif item.team_id != linked.team_id:
                raise ValueError(f"步骤 {item.step_id} 的队伍与派单不一致")
            if item.crew <= 0:
                item.crew = max(1, getattr(linked, "quantity", 0) or 1)
        if item.team_id is not None:
            team = self.teams.get(item.team_id)
            if item.event_id and team.region != self.events.get(item.event_id).region:
                raise ValueError(f"步骤 {item.step_id} 的队伍不在灾害点所在县")
        for lot_id, qty in item.lots.items():
            lot = self.supplies.get(lot_id)
            if qty <= 0:
                raise ValueError(f"步骤 {item.step_id} 占用物资数量必须为正")
            if lot.frozen:
                raise InvalidTransition(f"物资批次 {lot_id} 已冻结")

    def _step_from_spec(self, item: JointPlanStep):
        return {
            "step_id": item.step_id,
            "title": item.title,
            "priority": item.priority,
            "event_id": item.event_id,
            "assignment_id": item.assignment_id,
            "team_id": item.team_id,
            "crew": max(1, item.crew),
            "lots": dict(item.lots),
            "dependencies": set(item.dependencies),
            "state": "pending",
            "hold_reason": None,
            "blocked_by": [],
            "resources": [],
            "updated_at": None,
        }

    @staticmethod
    def _validate_graph(graph):
        visiting, visited = set(), set()

        def visit(node):
            if node not in graph:
                raise ValueError(f"依赖了不存在的步骤: {node}")
            if node in visiting:
                raise ValueError("行动依赖存在环")
            if node in visited:
                return
            visiting.add(node)
            for parent in graph.get(node, set()):
                visit(parent)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)

    # ------------------------------------------------------------------ 持久化

    def _insert_step_row(self, plan_id, step):
        self.store.execute(
            "INSERT INTO joint_steps(plan_id,step_id,title,priority,event_id,assignment_id,team_id,crew,"
            "dependencies,state,hold_reason,blocked_by,resources,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, step["step_id"], step["title"], step["priority"], step["event_id"],
             step["assignment_id"], step["team_id"], step["crew"],
             json.dumps(sorted(step["dependencies"]), ensure_ascii=False),
             step["state"], step["hold_reason"], json.dumps(step["blocked_by"], ensure_ascii=False),
             json.dumps(self._resource_view(step), ensure_ascii=False),
             step["updated_at"]),
        )
        for lot_id, qty in step["lots"].items():
            self.store.execute(
                "INSERT OR IGNORE INTO joint_step_lots(plan_id,step_id,lot_id,quantity) VALUES(?,?,?,?)",
                (plan_id, step["step_id"], lot_id, qty),
            )

    def _persist_step(self, plan_id, step):
        self.store.execute(
            "UPDATE joint_steps SET state=?, hold_reason=?, blocked_by=?, resources=?, updated_at=? "
            "WHERE plan_id=? AND step_id=?",
            (step["state"], step["hold_reason"], json.dumps(step["blocked_by"], ensure_ascii=False),
             json.dumps(self._resource_view(step), ensure_ascii=False), step["updated_at"],
             plan_id, step["step_id"]),
        )

    def _touch_plan(self, plan, state=None):
        if state is not None:
            plan["state"] = state
        plan["updated_at"] = _iso(utc_now())
        self.store.execute(
            "UPDATE joint_plans SET state=?, updated_at=? WHERE plan_id=?",
            (plan["state"], plan["updated_at"], plan["plan_id"]),
        )

    @staticmethod
    def _resource_view(step):
        """该步骤在当前状态下实际占用/消耗的资源。

        running：锁定中；completed：已随步骤完成而消耗（占用不回收）；
        pending/cancelled：无在账占用（取消时资源已释放）。
        """
        if step["state"] not in {"running", "completed"}:
            return []
        status = "held" if step["state"] == "running" else "consumed"
        used = []
        if step.get("team_id"):
            used.append({"type": "team", "id": step["team_id"], "amount": step["crew"], "status": status})
        for lot_id, qty in step.get("lots", {}).items():
            used.append({"type": "lot", "id": lot_id, "amount": qty, "status": status})
        return used

    # ---------------------------------------------------------- 调度（核心算法）

    def _snapshot(self, plan):
        """返回每个步骤的调度视图：是否可执行、为何等待、使用了哪项资源。"""
        steps = plan["steps"]
        running = [s for s in steps.values() if s["state"] == "running"]
        busy_teams: dict[str, str] = {}
        busy_lots: dict[str, str] = {}
        for s in running:
            if s["team_id"]:
                busy_teams[s["team_id"]] = s["step_id"]
            for lot_id in s["lots"]:
                busy_lots[lot_id] = s["step_id"]

        view = {}
        for sid, step in steps.items():
            blocked_by = []
            reasons = []
            if step["state"] in _TERMINAL_STATES:
                view[sid] = {"state": step["state"], "blocked_by": [], "reasons": [],
                             "resources": self._resource_view(step)}
                continue
            for dep in sorted(step["dependencies"]):
                dep_state = steps[dep]["state"]
                if dep_state == "cancelled":
                    reasons.append(f"前置步骤 {dep} 已取消，需要指挥员重排")
                    blocked_by.append(dep)
                elif dep_state != "completed":
                    reasons.append(f"等待前置步骤 {dep}（{dep_state}）完成")
                    blocked_by.append(dep)
            # 资源可用性只用于判断 pending 步骤能否启动；running 步骤已持有资源，不再自检
            if step["state"] != "running":
                if step["team_id"] and step["team_id"] in busy_teams:
                    holder = busy_teams[step["team_id"]]
                    if holder != sid:
                        reasons.append(f"队伍 {step['team_id']} 正被进行中的步骤 {holder} 占用")
                        blocked_by.append(holder)
                # 队伍剩余能力（含派单已占用）；重启后台账未重放时先提示等待恢复
                if step["team_id"]:
                    team = self.teams.teams.get(step["team_id"])
                    if team is None:
                        reasons.append(f"队伍 {step['team_id']} 的台账尚未恢复，等待重放")
                    elif team.capacity < step["crew"]:
                        reasons.append(f"队伍 {step['team_id']} 可用能力不足（需 {step['crew']}，剩 {team.capacity}）")
                for lot_id in step["lots"]:
                    holder = busy_lots.get(lot_id)
                    if holder and holder != sid:
                        reasons.append(f"物资批次 {lot_id} 正被进行中的步骤 {holder} 占用")
                        if holder not in blocked_by:
                            blocked_by.append(holder)
                # 库存可用性（冻结/余量不足）同样使步骤等待；
                # 重启后物资台账尚未重放时，先提示等待台账恢复而非直接报错
                for lot_id, qty in step["lots"].items():
                    lot = self.supplies.lots.get(lot_id)
                    if lot is None:
                        reasons.append(f"物资批次 {lot_id} 的台账尚未恢复，等待重放")
                    elif lot.frozen:
                        reasons.append(f"物资批次 {lot_id} 已冻结")
                    elif lot.quantity - lot.reserved < qty:
                        reasons.append(f"物资批次 {lot_id} 可用余量不足（需 {qty}）")
            view[sid] = {"state": step["state"], "blocked_by": blocked_by, "reasons": reasons,
                         "resources": self._resource_view(step)}
        return view

    def _order(self, plan, snapshot):
        """按 优先级降序、编号升序 给出当前可启动步骤的顺序。

        在途（running）步骤已持有资源，不重复进入待启动序列；
        它们的续办由 schedule 的 resume_from 指引。
        """
        ready = [sid for sid, info in snapshot.items()
                 if info["state"] == "pending" and not info["reasons"]]
        steps = plan["steps"]
        return sorted(ready, key=lambda sid: (-steps[sid]["priority"], sid))

    def schedule(self, plan_id):
        plan = self._get_plan(plan_id)
        snapshot = self._snapshot(plan)
        order = self._order(plan, snapshot)
        return {
            "plan_id": plan_id,
            "state": plan["state"],
            "execution_order": order,
            "steps": [
                {
                    "step_id": sid,
                    "title": plan["steps"][sid]["title"],
                    "priority": plan["steps"][sid]["priority"],
                    "state": snapshot[sid]["state"],
                    "waiting": bool(snapshot[sid]["reasons"]),
                    "wait_reasons": snapshot[sid]["reasons"],
                    "blocked_by": snapshot[sid]["blocked_by"],
                    "resources": snapshot[sid]["resources"],
                    "binds": self._binds(plan["steps"][sid]),
                }
                for sid in sorted(plan["steps"])
            ],
            "resume_from": self._resume_from(plan, order),
        }

    @staticmethod
    def _binds(step):
        return {"event_id": step["event_id"], "assignment_id": step["assignment_id"],
                "team_id": step["team_id"], "lots": dict(step["lots"])}

    @staticmethod
    def _resume_from(plan, order):
        """重启续办点：优先在途（running）步骤，其次可执行步骤中优先级最高者。

        计划暂停或进程重启后，指挥员据此知道“从哪里继续”：
        在途步骤已持有资源锁定，应先核实并确认；其余按优先级续办。
        """
        if plan["state"] in {"draft", "completed"}:
            return None
        running = sorted(
            (sid for sid, step in plan["steps"].items() if step["state"] == "running"),
            key=lambda sid: (-plan["steps"][sid]["priority"], sid),
        )
        if running:
            return running[0]
        return order[0] if order else None

    def describe(self, plan_id):
        """完整查询：每一步为何等待、使用了哪项资源、恢复后从哪里继续。"""
        return self.schedule(plan_id)

    # ------------------------------------------------------------------ 执行

    def start_plan(self, plan_id, request_id):
        plan = self._get_plan(plan_id)
        if plan["state"] not in {"draft", "paused"}:
            raise InvalidTransition("计划当前不能启动（仅草稿/已暂停可启动）")
        self._touch_plan(plan, "running")
        result = self.schedule(plan_id)
        self.audit.append("start_joint_plan", "joint_plan", plan_id, request_id,
                          {"ready": result["execution_order"], "resume_from": result["resume_from"]})
        return result

    def pause_plan(self, plan_id, request_id):
        plan = self._get_plan(plan_id)
        if plan["state"] != "running":
            raise InvalidTransition("只有执行中的联合行动计划可暂停")
        self._touch_plan(plan, "paused")
        self.audit.append("pause_joint_plan", "joint_plan", plan_id, request_id, {})
        return self.schedule(plan_id)

    def start_step(self, plan_id, step_id, request_id):
        plan = self._get_plan(plan_id)
        if plan["state"] != "running":
            raise InvalidTransition("计划未在执行中，步骤不能启动")
        step = self._get_step(plan, step_id)
        if step["state"] != "pending":
            raise InvalidTransition("步骤不是待执行状态")
        snapshot = self._snapshot(plan)
        if snapshot[step_id]["reasons"]:
            raise Conflict("；".join(snapshot[step_id]["reasons"]))

        step["state"] = "running"
        step["updated_at"] = _iso(utc_now())
        acquired_lots = {}
        try:
            if step["team_id"]:
                self.teams.consume(step["team_id"], step["crew"], request_id)
            for lot_id, qty in step["lots"].items():
                self.supplies.reserve(lot_id, qty, request_id)
                acquired_lots[lot_id] = qty
            if step["assignment_id"]:
                self.assignments.acknowledge(step["assignment_id"], request_id)
        except Exception:
            # 任一资源占用失败：回滚已取得的锁定，避免步骤未启动却留下半占用
            for lot_id, qty in acquired_lots.items():
                self.supplies.release(lot_id, qty, request_id)
            if step["team_id"]:
                self.teams.restore(step["team_id"], step["crew"], request_id)
            step["state"] = "pending"
            step["updated_at"] = None
            raise
        step["hold_reason"] = None
        step["blocked_by"] = []
        self._persist_step(plan_id, step)
        self.audit.append("start_joint_step", "joint_step", f"{plan_id}/{step_id}", request_id,
                          {"resources": self._resource_view(step)})
        return self.schedule(plan_id)

    def confirm_step(self, plan_id, step_id, request_id):
        """确认步骤完成。重复确认同一 request_id 或步骤已完成时幂等返回。"""
        plan = self._get_plan(plan_id)
        step = self._get_step(plan, step_id)
        if step["state"] == "completed":
            return self.schedule(plan_id)
        if step["state"] != "running":
            raise InvalidTransition("步骤尚未启动，不能确认完成")

        step["state"] = "completed"
        step["updated_at"] = _iso(utc_now())
        if step["assignment_id"]:
            self.assignments.complete(step["assignment_id"], request_id)
        self._persist_step(plan_id, step)
        if all(s["state"] == "completed" for s in plan["steps"].values()):
            self._touch_plan(plan, "completed")
        else:
            self._touch_plan(plan)
        self.audit.append("complete_joint_step", "joint_step", f"{plan_id}/{step_id}", request_id, {})
        return self.schedule(plan_id)

    def cancel_step(self, plan_id, step_id, request_id):
        """取消步骤。运行中的步骤先释放占用的队伍能力与物资锁定，且不触碰已完成步骤。"""
        plan = self._get_plan(plan_id)
        step = self._get_step(plan, step_id)
        if step["state"] in _TERMINAL_STATES:
            raise InvalidTransition("步骤已终结，不能取消")

        was_running = step["state"] == "running"
        released = self._resource_view(step) if was_running else []
        if was_running:
            for lot_id, qty in step["lots"].items():
                self.supplies.release(lot_id, qty, request_id)
            if step["team_id"]:
                self.teams.restore(step["team_id"], step["crew"], request_id)
        step["state"] = "cancelled"
        step["updated_at"] = _iso(utc_now())
        self._persist_step(plan_id, step)
        self._touch_plan(plan)
        # 计算受影响、可重排的后续步骤（仅报告，不改动其状态——已完成的永不回滚）
        descendants = self._descendants(plan, step_id)
        rerunnable = sorted(
            sid for sid in descendants
            if plan["steps"][sid]["state"] not in _TERMINAL_STATES
        )
        self.audit.append("cancel_joint_step", "joint_step", f"{plan_id}/{step_id}", request_id,
                          {"released": released, "rerunnable": rerunnable})
        result = self.schedule(plan_id)
        result["rerunnable_after_cancel"] = rerunnable
        return result

    def reschedule(self, plan_id, updated: list[JointPlanStep], request_id):
        """局部重排：只允许修改未启动（pending）的步骤，已完成/进行中的步骤保持不动。"""
        plan = self._get_plan(plan_id)
        steps = plan["steps"]
        incoming = {item.step_id: item for item in updated}

        untouched = set(steps) - set(incoming)
        for sid in incoming:
            if sid not in steps:
                raise NotFound(f"步骤 {sid} 不在计划中，重排不能新增步骤")
            if steps[sid]["state"] != "pending":
                raise InvalidTransition(f"步骤 {sid} 已启动或完成，不能重排")

        merged: dict[str, dict] = {}
        for sid, current in steps.items():
            if sid in incoming:
                item = incoming[sid]
                self._validate_resource_links(item)
                merged[sid] = self._step_from_spec(item)
            else:
                clone = dict(current)
                clone["dependencies"] = set(current["dependencies"])
                clone["lots"] = dict(current["lots"])
                merged[sid] = clone

        graph = {sid: step["dependencies"] for sid, step in merged.items()}
        self._validate_graph(graph)

        plan_id = plan["plan_id"]
        self.store.execute("DELETE FROM joint_steps WHERE plan_id=?", (plan_id,))
        self.store.execute("DELETE FROM joint_step_lots WHERE plan_id=?", (plan_id,))
        for step in merged.values():
            self._insert_step_row(plan_id, step)
        plan["steps"] = merged
        self._touch_plan(plan)
        self.audit.append("reschedule_joint_plan", "joint_plan", plan_id, request_id,
                          {"updated": sorted(incoming), "untouched": sorted(untouched)})
        return self.schedule(plan_id)

    def _descendants(self, plan, step_id):
        result = set()
        stack = [step_id]
        while stack:
            current = stack.pop()
            for sid, step in plan["steps"].items():
                if current in step["dependencies"] and sid not in result:
                    result.add(sid)
                    stack.append(sid)
        return result

    @staticmethod
    def _get_step(plan, step_id):
        if step_id not in plan["steps"]:
            raise NotFound("步骤不存在")
        return plan["steps"][step_id]


# ------------------------------------------------------------------ 请求解析

def step_from_dict(payload: dict) -> JointPlanStep:
    return JointPlanStep(
        step_id=str(payload["step_id"]),
        title=str(payload.get("title", payload["step_id"])),
        priority=int(payload.get("priority", 0)),
        event_id=payload.get("event_id"),
        assignment_id=payload.get("assignment_id"),
        team_id=payload.get("team_id"),
        crew=int(payload.get("crew", 1)),
        lots={str(lot_id): int(qty) for lot_id, qty in payload.get("lots", {}).items()},
        dependencies={str(dep) for dep in payload.get("dependencies", [])},
    )


def plan_from_dict(payload: dict) -> JointPlan:
    return JointPlan(
        plan_id=str(payload["plan_id"]),
        region=str(payload["region"]),
        event_ids=[str(eid) for eid in payload["event_ids"]],
        steps=[step_from_dict(item) for item in payload.get("steps", [])],
    )
