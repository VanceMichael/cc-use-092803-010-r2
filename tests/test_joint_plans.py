import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from emergency_service.auth import Principal
from emergency_service.clock import utc_now
from emergency_service.errors import InvalidTransition, PermissionDenied
from emergency_service.models import Assignment, Event, SupplyLot, Team
from emergency_service.service import EmergencyPlatform


def step(step_id, event_id, assignment_id, priority=100, depends_on=None, supplies=None):
    return {"step_id": step_id, "event_id": event_id, "assignment_id": assignment_id,
            "priority": priority, "depends_on": depends_on or [], "supplies": supplies or []}


class JointPlanTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.p = EmergencyPlatform(self.db)
        self.admin = Principal("cmd-1", frozenset({"commander", "dispatcher", "read_audit"}), frozenset({"global"}))
        self.dispatcher = Principal("disp-1", frozenset({"dispatcher"}), frozenset({"global"}))
        now = datetime.now(timezone.utc)
        # 两个县的灾害点
        self.p.create_event(self.admin, Event("e-cq", "重庆", "landslide", 4, now, "sensor-cq"), "req-e1")
        self.p.create_event(self.admin, Event("e-sc", "四川", "flood", 3, now, "sensor-sc"), "req-e2")
        # 三支队伍
        self.p.teams.register(Team("t-a", "重庆", {"rescue"}, 4), "r-t1")
        self.p.teams.register(Team("t-b", "四川", {"rescue"}, 4), "r-t2")
        self.p.teams.register(Team("t-c", "四川", {"medical"}, 4), "r-t3")
        # 两批物资
        self.p.supplies.add(SupplyLot("l-1", "帐篷", 10), "r-l1")
        self.p.supplies.add(SupplyLot("l-2", "药品", 5), "r-l2")
        self.p._persist()
        # 四张派单
        self.p.assign(self.admin, Assignment("a1", "e-cq", "t-a", quantity=1), "r-a1")
        self.p.assign(self.admin, Assignment("a2", "e-sc", "t-b", quantity=1), "r-a2")
        self.p.assign(self.admin, Assignment("a3", "e-sc", "t-b", quantity=1), "r-a3")
        self.p.assign(self.admin, Assignment("a4", "e-sc", "t-c", quantity=1), "r-a4")

    def _plan(self, plan_id="jp-1", region="跨县", steps=None, request_id="jp-create"):
        steps = steps if steps is not None else [
            step("s1", "e-cq", "a1", priority=10, supplies=[{"lot_id": "l-1", "quantity": 3}]),
            step("s2", "e-sc", "a2", priority=20, depends_on=["s1"], supplies=[{"lot_id": "l-2", "quantity": 2}]),
            step("s3", "e-sc", "a3", priority=5, depends_on=["s1"]),
            step("s4", "e-sc", "a4", priority=30, depends_on=["s2", "s3"]),
        ]
        return self.p.create_joint_plan(self.admin, plan_id, region, steps, request_id)

    def test_create_locks_resources_and_reports_order_and_waiting(self):
        view = self._plan()
        # 物资被计划预留
        self.assertEqual(self.p.supplies.get("l-1").reserved, 3)
        self.assertEqual(self.p.supplies.get("l-2").reserved, 2)
        # 派单被锁定，不能再在计划外操作
        with self.assertRaises(InvalidTransition):
            self.p.acknowledge(self.admin, "a1", "rogue-ack")
        s1, s2, s3, s4 = (next(s for s in view["steps"] if s["step_id"] == sid) for sid in ("s1", "s2", "s3", "s4"))
        self.assertEqual(s1["state"], "ready")
        self.assertEqual(s1["waiting_reason"], "可执行")
        self.assertEqual(s1["resources"]["team_id"], "t-a")
        self.assertEqual(s1["resources"]["supplies"][0]["lot_id"], "l-1")
        self.assertTrue(any("等待前序步骤 s1" in r for r in s2["blocked_by"]))
        self.assertTrue(any("等待前序步骤 s1" in r for r in s3["blocked_by"]))
        self.assertTrue(any("s2" in r or "s3" in r for r in s4["blocked_by"]))
        # 只有 s1 可执行
        self.assertEqual(view["executable_order"], ["s1"])

    def test_team_conflict_resolved_by_priority(self):
        # s2、s3 同用 t-b，且无依赖关系；优先级高的 s3 先执行，s2 等待
        view = self.p.create_joint_plan(self.admin, "jp-x", "跨县", [
            step("s2", "e-sc", "a2", priority=20),
            step("s3", "e-sc", "a3", priority=5),
        ], "jp-x")
        by_id = {s["step_id"]: s for s in view["steps"]}
        self.assertEqual(by_id["s3"]["state"], "ready")
        self.assertEqual(by_id["s2"]["state"], "blocked")
        self.assertIn("优先级更高的步骤 s3", by_id["s2"]["waiting_reason"])

    def test_create_rolls_back_on_insufficient_supply(self):
        with self.assertRaises(InvalidTransition):
            self.p.create_joint_plan(self.admin, "jp-bad", "跨县", [
                step("s1", "e-cq", "a1", supplies=[{"lot_id": "l-1", "quantity": 100}]),
            ], "jp-bad")
        self.assertEqual(self.p.supplies.get("l-1").reserved, 0)
        self.assertIsNone(self.p.assignments.get("a1").plan_id)
        self.assertNotIn("jp-bad", self.p.joint_plans.plans)

    def test_pause_blocks_dispatch_and_resume_continues(self):
        self._plan()
        self.p.start_joint_plan(self.admin, "jp-1", "jp-start")
        self.p.confirm_step(self.admin, "jp-1", "s1", "cf-s1")
        self.assertEqual(self.p.assignments.get("a1").state, "acknowledged")
        paused = self.p.pause_joint_plan(self.admin, "jp-1", "jp-pause")
        self.assertEqual(paused["state"], "paused")
        # 暂停期间即使依赖满足也不能确认
        with self.assertRaises(InvalidTransition):
            self.p.confirm_step(self.admin, "jp-1", "s3", "cf-s3-paused")
        resumed = self.p.start_joint_plan(self.admin, "jp-1", "jp-resume")
        self.assertEqual(resumed["state"], "running")
        # 恢复点指向执行中的 s1
        self.assertEqual(resumed["resume_from"], ["s1"])

    def test_duplicate_confirm_is_idempotent(self):
        self._plan()
        self.p.start_joint_plan(self.admin, "jp-1", "jp-start")
        first = self.p.confirm_step(self.admin, "jp-1", "s1", "cf-s1")
        again = self.p.confirm_step(self.admin, "jp-1", "s1", "cf-s1")
        self.assertEqual(first, again)
        step_view = next(s for s in again["steps"] if s["step_id"] == "s1")
        self.assertEqual(step_view["attempts"], 1)
        self.assertEqual(step_view["state"], "running")
        # 换 request_id 的重复确认也只回放，不重复推进派单
        retried = self.p.confirm_step(self.admin, "jp-1", "s1", "cf-s1-retry")
        self.assertTrue(retried.get("idempotent"))
        self.assertEqual(self.p.assignments.get("a1").state, "acknowledged")

    def test_complete_consumes_supplies_and_unlocks_downstream(self):
        self._plan()
        self.p.start_joint_plan(self.admin, "jp-1", "jp-start")
        self.p.confirm_step(self.admin, "jp-1", "s1", "cf-s1")
        view = self.p.complete_step(self.admin, "jp-1", "s1", "cp-s1")
        # 预留核销：库存 10→7，预留归零
        lot = self.p.supplies.get("l-1")
        self.assertEqual(lot.quantity, 7)
        self.assertEqual(lot.reserved, 0)
        self.assertEqual(self.p.assignments.get("a1").state, "completed")
        # s2、s3 依赖解除，s3 优先级高；同队 t-b 下 s2 等待
        by_id = {s["step_id"]: s for s in view["steps"]}
        self.assertEqual(by_id["s3"]["state"], "ready")
        self.assertEqual(by_id["s2"]["state"], "blocked")
        # 已完成步骤再发完成请求是幂等回放
        again = self.p.complete_step(self.admin, "jp-1", "s1", "cp-s1-retry")
        self.assertTrue(again.get("idempotent"))
        self.assertEqual(self.p.supplies.get("l-1").quantity, 7)

    def test_cancel_releases_resources_and_marks_downstream(self):
        view = self._plan()
        self.p.start_joint_plan(self.admin, "jp-1", "jp-start")
        view = self.p.cancel_step(self.admin, "jp-1", "s1", "cancel-s1")
        # 释放物资、恢复队伍能力
        self.assertEqual(self.p.supplies.get("l-1").reserved, 0)
        self.assertEqual(self.p.teams.get("t-a").capacity, 4)
        self.assertEqual(self.p.assignments.get("a1").state, "cancelled")
        self.assertIsNone(self.p.assignments.get("a1").plan_id)
        # s2、s3 因前序取消而需要重排，s4 经传递也被标出
        self.assertEqual(set(view["reschedulable"]["s1"]), {"s2", "s3", "s4"})
        by_id = {s["step_id"]: s for s in view["steps"]}
        self.assertIn("已取消，需要重排依赖", by_id["s2"]["waiting_reason"])
        # 局部重排：解除 s3 对已取消 s1 的依赖后继续执行，已取消的 s1 不被触碰
        view = self.p.reschedule(self.admin, "jp-1", {"s3": {"depends_on": []}}, [], "rs-after-cancel")
        self.assertEqual(next(s for s in view["steps"] if s["step_id"] == "s1")["state"], "cancelled")
        self.p.confirm_step(self.admin, "jp-1", "s3", "cf-s3")
        self.p.complete_step(self.admin, "jp-1", "s3", "cp-s3")
        # 已完成步骤不可取消
        with self.assertRaises(InvalidTransition):
            self.p.cancel_step(self.admin, "jp-1", "s3", "cancel-s3")

    def test_reschedule_only_touches_pending_steps(self):
        self._plan()
        self.p.start_joint_plan(self.admin, "jp-1", "jp-start")
        self.p.confirm_step(self.admin, "jp-1", "s1", "cf-s1")
        self.p.complete_step(self.admin, "jp-1", "s1", "cp-s1")
        self.p.confirm_step(self.admin, "jp-1", "s3", "cf-s3")
        self.p.complete_step(self.admin, "jp-1", "s3", "cp-s3")
        # s1 已完成，改它必须被拒绝
        with self.assertRaises(InvalidTransition):
            self.p.reschedule(self.admin, "jp-1", {"s1": {"priority": 1}}, [], "rs-deny")
        # 把 s2 的依赖改到已完成的 s3 并提高优先级：s2 立即可执行，已完成步骤不变
        view = self.p.reschedule(self.admin, "jp-1", {"s2": {"depends_on": ["s3"], "priority": 1}}, [], "rs-1")
        by_id = {s["step_id"]: s for s in view["steps"]}
        self.assertEqual(by_id["s2"]["depends_on"], ["s3"])
        self.assertEqual(by_id["s2"]["state"], "ready")
        self.assertEqual(by_id["s1"]["state"], "completed")
        # 重排失败（a1 已随 s1 完成，不能再锁定）时整体回滚，s2 的调整也不留残痕
        with self.assertRaises(InvalidTransition):
            self.p.reschedule(self.admin, "jp-1", {"s2": {"priority": 99}},
                              [step("s5", "e-cq", "a1")], "rs-2")
        view = self.p.joint_plan_view(self.admin, "jp-1")
        self.assertEqual(next(s for s in view["steps"] if s["step_id"] == "s2")["priority"], 1)

    def test_restart_rebuilds_and_resumes_from_running_step(self):
        self._plan()
        self.p.start_joint_plan(self.admin, "jp-1", "jp-start")
        self.p.confirm_step(self.admin, "jp-1", "s1", "cf-s1")
        self.p.complete_step(self.admin, "jp-1", "s1", "cp-s1")
        self.p.confirm_step(self.admin, "jp-1", "s3", "cf-s3")
        # 模拟服务重启：新建实例指向同一数据库文件
        restarted = EmergencyPlatform(self.db)
        view = restarted.joint_plan_view(self.admin, "jp-1")
        self.assertEqual(view["state"], "running")
        by_id = {s["step_id"]: s for s in view["steps"]}
        self.assertEqual(by_id["s1"]["state"], "completed")
        self.assertEqual(by_id["s3"]["state"], "running")
        # 续办点是执行中的 s3；s2 因同队冲突仍等待
        self.assertEqual(view["resume_from"], ["s3"])
        self.assertIn("s3", by_id["s2"]["waiting_reason"])
        # 物资账与派单状态也完整恢复
        self.assertEqual(restarted.supplies.get("l-1").quantity, 7)
        self.assertEqual(restarted.assignments.get("a3").state, "acknowledged")
        # 重启后继续推进直到计划完成
        restarted.complete_step(self.admin, "jp-1", "s3", "cp-s3-2")
        restarted.confirm_step(self.admin, "jp-1", "s2", "cf-s2-2")
        restarted.complete_step(self.admin, "jp-1", "s2", "cp-s2-2")
        restarted.confirm_step(self.admin, "jp-1", "s4", "cf-s4-2")
        done = restarted.complete_step(self.admin, "jp-1", "s4", "cp-s4-2")
        self.assertEqual(done["state"], "completed")
        # 审计在重启后序号连续、不重复
        seqs = [e.sequence for e in restarted.audit.entries]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_idempotent_request_replay_after_restart(self):
        self._plan(request_id="jp-cache")
        first = self.p.start_joint_plan(self.admin, "jp-1", "start-cache")
        restarted = EmergencyPlatform(self.db)
        again = restarted.start_joint_plan(self.admin, "jp-1", "start-cache")
        self.assertEqual(first, again)

    def test_only_commander_can_manage_joint_plan(self):
        with self.assertRaises(PermissionDenied):
            self.p.create_joint_plan(self.dispatcher, "jp-z", "跨县", [
                step("s1", "e-cq", "a1")], "jp-z")

    def test_cross_region_commander_must_cover_all_events(self):
        local = Principal("cmd-sc", frozenset({"commander"}), frozenset({"四川"}))
        with self.assertRaises(PermissionDenied):
            self.p.create_joint_plan(local, "jp-r", "跨县", [
                step("s1", "e-cq", "a1")], "jp-r")

    def test_frozen_supply_blocks_ready(self):
        self._plan()
        self.p.supplies.freeze("l-2", "freeze-l2")
        self.p._persist()
        self.p.start_joint_plan(self.admin, "jp-1", "jp-start")
        self.p.confirm_step(self.admin, "jp-1", "s1", "cf-s1")
        self.p.complete_step(self.admin, "jp-1", "s1", "cp-s1")
        view = self.p.joint_plan_view(self.admin, "jp-1")
        s2 = next(s for s in view["steps"] if s["step_id"] == "s2")
        self.assertIn("物资批次 l-2 已冻结", s2["waiting_reason"])

    def test_audit_trail_records_joint_lifecycle(self):
        self._plan()
        actions = {e.action for e in self.p.audit.for_entity("joint_plan", "jp-1")}
        self.assertIn("create_joint_plan", actions)
        self.assertIn("bind_assignment", {e.action for e in self.p.audit.for_entity("assignment", "a1")})
        self.assertEqual(self.p.audit.entries[-1].actor, "cmd-1")

    def test_legacy_single_point_semantics_unchanged(self):
        # 原有独立派单/确认/取消链路不受影响
        self.p.assign(self.admin, Assignment("a-legacy", "e-cq", "t-a", quantity=2), "legacy-assign")
        # t-a 在 setUp 中已为 a1 消耗 1，再消耗 2 后余 1
        self.assertEqual(self.p.teams.get("t-a").capacity, 1)
        self.p.acknowledge(self.admin, "a-legacy", "legacy-ack")
        fresh = EmergencyPlatform(":memory:")
        fresh.create_event(self.admin, Event("e", "重庆", "rain", 1, utc_now(), "s"), "e")
        fresh.teams.register(Team("t", "重庆", set(), 2), "t")
        fresh.assign(self.admin, Assignment("a", "e", "t", quantity=1), "a")
        fresh.acknowledge(self.admin, "a", "ack")
        # 未被计划锁定的派单仍可直接取消并恢复队伍能力
        fresh.assignments.cancel("a", "cancel")
        item = fresh.assignments.get("a")
        self.assertEqual(item.state, "cancelled")
        self.assertEqual(fresh.teams.get("t").capacity, 2)


if __name__ == "__main__":
    unittest.main()
