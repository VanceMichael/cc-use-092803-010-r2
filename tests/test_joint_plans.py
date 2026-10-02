import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from emergency_service.auth import Principal
from emergency_service.errors import (
    Conflict, InvalidTransition, PermissionDenied,
)
from emergency_service.joint_plans import JointPlanBook, plan_from_dict
from emergency_service.models import Assignment, Event, SupplyLot, Team
from emergency_service.service import EmergencyPlatform
from emergency_service.store import StateStore


def make_spec(plan_id="jp1", steps=None):
    payload = {
        "plan_id": plan_id,
        "region": "重庆",
        "event_ids": ["e1", "e2"],
        "steps": steps if steps is not None else [
            {"step_id": "s1", "title": "勘察", "priority": 1, "event_id": "e1",
             "assignment_id": "a1", "team_id": "t1", "crew": 2, "lots": {"L1": 3}},
            {"step_id": "s2", "title": "转运", "priority": 5, "event_id": "e2",
             "assignment_id": "a2", "team_id": "t2", "crew": 1, "lots": {"L1": 4},
             "dependencies": ["s1"]},
            {"step_id": "s3", "title": "排险", "priority": 3, "event_id": "e1",
             "team_id": "t1", "crew": 2, "lots": {"L2": 1}},
        ],
    }
    return plan_from_dict(payload)


class JointPlanTest(unittest.TestCase):
    def setUp(self):
        self.p = EmergencyPlatform()
        self.commander = Principal("cmd", frozenset({"commander", "dispatcher"}), frozenset({"global"}))
        self.now = datetime.now(timezone.utc)
        self.p.events.create(Event("e1", "重庆", "rain", 3, self.now, "sensor-a"), "ev1")
        self.p.events.create(Event("e2", "成都", "rain", 2, self.now, "sensor-b"), "ev2")
        self.p.teams.register(Team("t1", "重庆", {"rescue"}, 5), "tm1")
        self.p.teams.register(Team("t2", "成都", {"rescue"}, 5), "tm2")
        self.p.supplies.add(SupplyLot("L1", "tents", 10), "sp1")
        self.p.supplies.add(SupplyLot("L2", "rope", 2), "sp2")
        self.p.assignments.plan(Assignment("a1", "e1", "t1", quantity=1), "as1")
        self.p.assignments.plan(Assignment("a2", "e2", "t2", quantity=1), "as2")

    def _by_id(self, view):
        return {item["step_id"]: item for item in view["steps"]}

    # --------------------------------------------------------- 建图与调度

    def test_create_binds_events_assignments_and_lots(self):
        view = self.p.create_joint_plan(self.commander, make_spec(), "jp-req-1")
        self.assertEqual(view["state"], "draft")
        s1 = self._by_id(view)["s1"]
        self.assertEqual(s1["binds"]["event_id"], "e1")
        self.assertEqual(s1["binds"]["assignment_id"], "a1")
        self.assertEqual(s1["binds"]["lots"], {"L1": 3})

    def test_dependency_wait_and_priority_order(self):
        view = self.p.create_joint_plan(self.commander, make_spec(), "jp-req-2")
        # s2 优先级最高但被 s1 阻塞；可执行集合按优先级排序为 s3, s1
        self.assertEqual(view["execution_order"], ["s3", "s1"])
        s2 = self._by_id(view)["s2"]
        self.assertTrue(s2["waiting"])
        self.assertIn("等待前置步骤 s1（pending）完成", s2["wait_reasons"])
        self.assertEqual(s2["blocked_by"], ["s1"])

    def test_team_conflict_makes_step_wait(self):
        spec = plan_from_dict({
            "plan_id": "jp2", "region": "重庆", "event_ids": ["e1"],
            "steps": [
                {"step_id": "x1", "priority": 2, "event_id": "e1", "team_id": "t1", "crew": 3},
                {"step_id": "x2", "priority": 5, "event_id": "e1", "team_id": "t1", "crew": 3},
            ],
        })
        self.p.create_joint_plan(self.commander, spec, "jp-req-3")
        self.p.start_joint_plan(self.commander, "jp2", "run-3")
        self.p.start_joint_step(self.commander, "jp2", "x1", "st-x1")
        view = self.p.get_joint_plan(self.commander, "jp2")
        x2 = self._by_id(view)["x2"]
        self.assertTrue(x2["waiting"])
        self.assertIn("队伍 t1 正被进行中的步骤 x1 占用", x2["wait_reasons"])

    def test_supply_shortage_makes_step_wait(self):
        spec = plan_from_dict({
            "plan_id": "jp3", "region": "重庆", "event_ids": ["e1"],
            "steps": [{"step_id": "y1", "priority": 1, "event_id": "e1",
                       "team_id": "t1", "crew": 1, "lots": {"L2": 5}}],
        })
        self.p.create_joint_plan(self.commander, spec, "jp-req-4")
        self.p.start_joint_plan(self.commander, "jp3", "run-4")
        view = self.p.get_joint_plan(self.commander, "jp3")
        y1 = self._by_id(view)["y1"]
        self.assertIn("物资批次 L2 可用余量不足（需 5）", y1["wait_reasons"])
        with self.assertRaises(Conflict):
            self.p.start_joint_step(self.commander, "jp3", "y1", "st-y1")

    def test_cycle_is_rejected(self):
        spec = make_spec("jp4")
        spec.steps[0].dependencies = {"s2"}
        with self.assertRaises(ValueError):
            self.p.create_joint_plan(self.commander, spec, "jp-req-5")

    def test_step_event_must_belong_to_plan(self):
        spec = make_spec("jp5")
        spec.steps[0].event_id = "e-other"
        with self.assertRaises(Exception):
            self.p.create_joint_plan(self.commander, spec, "jp-req-6")

    # --------------------------------------------------------- 执行与资源

    def test_start_step_locks_resources_and_confirm_consumes(self):
        self.p.create_joint_plan(self.commander, make_spec(), "jp-req-7")
        self.p.start_joint_plan(self.commander, "jp1", "run-7")
        view = self.p.start_joint_step(self.commander, "jp1", "s3", "st-s3")
        s3 = self._by_id(view)["s3"]
        self.assertEqual(s3["state"], "running")
        # 资源被占用：队伍能力扣减 2（派单 a1 先前已扣 1），L2 锁定 1
        self.assertEqual(self.p.teams.get("t1").capacity, 2)
        self.assertEqual(self.p.supplies.get("L2").reserved, 1)
        held = {r["type"] + ":" + r["id"]: r for r in s3["resources"]}
        self.assertEqual(held["team:t1"]["status"], "held")

        done = self.p.confirm_joint_step(self.commander, "jp1", "s3", "cf-s3")
        self.assertEqual(self._by_id(done)["s3"]["state"], "completed")
        used = self._by_id(done)["s3"]["resources"]
        self.assertTrue(any(r["type"] == "lot" and r["id"] == "L2" and r["status"] == "consumed" for r in used))

    def test_start_step_links_assignment_lifecycle(self):
        self.p.create_joint_plan(self.commander, make_spec(), "jp-req-8")
        self.p.start_joint_plan(self.commander, "jp1", "run-8")
        self.p.start_joint_step(self.commander, "jp1", "s3", "st-s3b")
        self.p.confirm_joint_step(self.commander, "jp1", "s3", "cf-s3b")
        self.p.start_joint_step(self.commander, "jp1", "s1", "st-s1")
        self.assertEqual(self.p.assignments.get("a1").state, "acknowledged")
        self.p.confirm_joint_step(self.commander, "jp1", "s1", "cf-s1")
        self.assertEqual(self.p.assignments.get("a1").state, "completed")
        # s1 完成后，被依赖的 s2 进入可执行序列且排第一（优先级 5）
        view = self.p.get_joint_plan(self.commander, "jp1")
        self.assertEqual(view["execution_order"], ["s2"])

    def test_failed_resource_lock_rolls_back(self):
        from emergency_service.errors import CapacityExceeded
        self.p.create_joint_plan(self.commander, make_spec(), "jp-req-9")
        self.p.start_joint_plan(self.commander, "jp1", "run-9")
        # 模拟“队伍能力已扣减、物资锁定却失败”的运行时故障
        original_reserve = self.p.supplies.reserve

        def boom(lot_id, amount, request_id):
            raise CapacityExceeded("模拟物资锁定失败")

        self.p.supplies.reserve = boom
        try:
            with self.assertRaises(CapacityExceeded):
                self.p.start_joint_step(self.commander, "jp1", "s3", "st-s3c")
        finally:
            self.p.supplies.reserve = original_reserve
        # 补偿回滚：队伍能力恢复（派单占用后的 4），步骤仍是 pending
        self.assertEqual(self.p.teams.get("t1").capacity, 4)
        self.assertEqual(self._by_id(self.p.get_joint_plan(self.commander, "jp1"))["s3"]["state"], "pending")

    # --------------------------------------------------------- 幂等

    def test_repeated_requests_are_idempotent(self):
        first = self.p.create_joint_plan(self.commander, make_spec(), "same-req")
        second = self.p.create_joint_plan(self.commander, make_spec(), "same-req")
        self.assertEqual(first, second)
        self.p.start_joint_plan(self.commander, "jp1", "run-x")
        self.p.start_joint_step(self.commander, "jp1", "s3", "st-x")
        once = self.p.confirm_joint_step(self.commander, "jp1", "s3", "cf-x")
        again = self.p.confirm_joint_step(self.commander, "jp1", "s3", "cf-x")
        self.assertEqual(once, again)
        # 换 request_id 对已完成步骤重复确认同样幂等，且不重复记账
        reserved_after = self.p.supplies.get("L2").reserved
        self.p.confirm_joint_step(self.commander, "jp1", "s3", "cf-x-retry")
        self.assertEqual(self.p.supplies.get("L2").reserved, reserved_after)

    # --------------------------------------------------------- 取消与局部重排

    def test_cancel_running_step_releases_and_reports_rerunnable(self):
        self.p.create_joint_plan(self.commander, make_spec(), "jp-req-10")
        self.p.start_joint_plan(self.commander, "jp1", "run-10")
        self.p.start_joint_step(self.commander, "jp1", "s1", "st-s1r")
        result = self.p.cancel_joint_step(self.commander, "jp1", "s1", "cl-s1")
        # 资源释放：能力回到派单占用后的 4，L1 锁定归零，派单不动账
        self.assertEqual(self.p.teams.get("t1").capacity, 4)
        self.assertEqual(self.p.supplies.get("L1").reserved, 0)
        self.assertEqual(self.p.assignments.get("a1").state, "acknowledged")
        self.assertEqual(result["rerunnable_after_cancel"], ["s2"])
        s2 = self._by_id(result)["s2"]
        self.assertTrue(any("前置步骤 s1 已取消" in r for r in s2["wait_reasons"]))

    def test_completed_steps_are_never_rolled_back(self):
        self.p.create_joint_plan(self.commander, make_spec(), "jp-req-11")
        self.p.start_joint_plan(self.commander, "jp1", "run-11")
        self.p.start_joint_step(self.commander, "jp1", "s3", "st-s3d")
        self.p.confirm_joint_step(self.commander, "jp1", "s3", "cf-s3d")
        with self.assertRaises(InvalidTransition):
            self.p.cancel_joint_step(self.commander, "jp1", "s3", "cl-s3")
        updated = plan_from_dict({
            "plan_id": "jp1", "region": "重庆", "event_ids": ["e1", "e2"],
            "steps": [{
                "step_id": "s3", "title": "排险改", "priority": 9, "event_id": "e1",
                "team_id": "t1", "crew": 2, "lots": {"L2": 1},
            }],
        }).steps
        with self.assertRaises(InvalidTransition):
            self.p.reschedule_joint_plan(self.commander, "jp1", updated, "rs-1")

    def test_local_reschedule_only_touches_pending_steps(self):
        self.p.create_joint_plan(self.commander, make_spec(), "jp-req-12")
        self.p.start_joint_plan(self.commander, "jp1", "run-12")
        self.p.start_joint_step(self.commander, "jp1", "s3", "st-s3e")
        # 仅重排尚未启动的 s1：改优先级、去掉 s2 对它的依赖
        updated = plan_from_dict({
            "plan_id": "jp1", "region": "重庆", "event_ids": ["e1", "e2"],
            "steps": [
                {"step_id": "s1", "title": "勘察", "priority": 1, "event_id": "e1",
                 "assignment_id": "a1", "team_id": "t1", "crew": 2, "lots": {"L1": 3}},
                {"step_id": "s2", "title": "转运改", "priority": 8, "event_id": "e2",
                 "assignment_id": "a2", "team_id": "t2", "crew": 1, "lots": {"L1": 4}},
            ],
        }).steps
        view = self.p.reschedule_joint_plan(self.commander, "jp1", updated, "rs-2")
        steps = self._by_id(view)
        self.assertEqual(steps["s3"]["state"], "running")  # 在途步骤不受影响
        self.assertEqual(steps["s2"]["title"], "转运改")
        self.assertFalse(steps["s2"]["waiting"])  # 依赖解除
        # s2 可立即启动；s1 因 t1 仍被在途的 s3 占用而等待——在途锁不被重排破坏
        self.assertEqual(view["execution_order"], ["s2"])
        self.assertTrue(any("队伍 t1 正被进行中的步骤 s3 占用" in r for r in steps["s1"]["wait_reasons"]))

    # --------------------------------------------------------- 暂停/重启续办

    def test_pause_blocks_start_and_resume_reports_point(self):
        self.p.create_joint_plan(self.commander, make_spec(), "jp-req-13")
        self.p.start_joint_plan(self.commander, "jp1", "run-13")
        self.p.start_joint_step(self.commander, "jp1", "s3", "st-s3f")
        paused = self.p.pause_joint_plan(self.commander, "jp1", "ps-1")
        self.assertEqual(paused["state"], "paused")
        with self.assertRaises(InvalidTransition):
            self.p.start_joint_step(self.commander, "jp1", "s1", "st-s1p")
        recovery = self.p.recover_joint_plans(self.commander)
        self.assertEqual(recovery[0]["plan_id"], "jp1")
        self.assertEqual(recovery[0]["resume_from"], "s3")  # 在途步骤优先续办
        self.assertEqual(recovery[0]["in_flight_steps"], ["s3"])

    def test_plan_survives_restart_from_sqlite(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            p1 = EmergencyPlatform(path)
            p1.events.create(Event("e1", "重庆", "rain", 3, self.now, "sensor-a"), "ev1")
            p1.teams.register(Team("t1", "重庆", {"rescue"}, 5), "tm1")
            p1.supplies.add(SupplyLot("L2", "rope", 2), "sp2")
            spec = plan_from_dict({
                "plan_id": "jp9", "region": "重庆", "event_ids": ["e1"],
                "steps": [{"step_id": "z1", "priority": 2, "event_id": "e1",
                           "team_id": "t1", "crew": 1, "lots": {"L2": 1}}],
            })
            cmd = self.commander
            p1.create_joint_plan(cmd, spec, "c1")
            p1.start_joint_plan(cmd, "jp9", "r1")
            p1.start_joint_step(cmd, "jp9", "z1", "z-start")
            p1.pause_joint_plan(cmd, "jp9", "z-pause")

            # 模拟进程重启：新平台、新台账（上游重放后重新登记）、同一个 SQLite 文件
            p2 = EmergencyPlatform(path)
            p2.events.create(Event("e1", "重庆", "rain", 3, self.now, "sensor-a"), "ev1b")
            p2.teams.register(Team("t1", "重庆", {"rescue"}, 5), "tm1b")
            p2.supplies.add(SupplyLot("L2", "rope", 2), "sp2b")
            recovery = p2.recover_joint_plans(cmd)
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0]["plan_id"], "jp9")
            self.assertEqual(recovery[0]["state"], "paused")
            self.assertEqual(recovery[0]["resume_from"], "z1")
            view = p2.get_joint_plan(cmd, "jp9")
            self.assertEqual(self._by_id(view)["z1"]["state"], "running")
        finally:
            os.unlink(path)

    def test_book_level_restart_can_continue(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store1 = StateStore(path)
            book1 = JointPlanBook(
                store1, self.p.events, self.p.teams, self.p.supplies, self.p.assignments, self.p.audit
            )
            book1.create(make_spec("jpA"), "b1")
            book1.start_plan("jpA", "b2")
            book1.start_step("jpA", "s3", "b3")

            store2 = StateStore(path)
            book2 = JointPlanBook(
                store2, self.p.events, self.p.teams, self.p.supplies, self.p.assignments, self.p.audit
            )
            view = book2.schedule("jpA")
            self.assertEqual(view["resume_from"], "s3")
            done = book2.confirm_step("jpA", "s3", "b4")
            self.assertEqual(self._by_id(done)["s3"]["state"], "completed")
            store1.close()
            store2.close()
        finally:
            os.unlink(path)

    # --------------------------------------------------------- 鉴权与审计

    def test_cross_county_requires_permission_on_every_region(self):
        chongqing_only = Principal("local", frozenset({"commander"}), frozenset({"重庆"}))
        with self.assertRaises(PermissionDenied):
            self.p.create_joint_plan(chongqing_only, make_spec(), "jp-denied")

    def test_audit_records_joint_plan_lifecycle(self):
        self.p.create_joint_plan(self.commander, make_spec(), "jp-audit")
        self.p.start_joint_plan(self.commander, "jp1", "run-audit")
        self.p.start_joint_step(self.commander, "jp1", "s3", "st-audit")
        self.p.confirm_joint_step(self.commander, "jp1", "s3", "cf-audit")
        actions = [e.action for e in self.p.audit.for_entity("joint_plan", "jp1")]
        self.assertEqual(actions, ["create_joint_plan", "start_joint_plan"])
        step_actions = [e.action for e in self.p.audit.for_entity("joint_step", "jp1/s3")]
        self.assertEqual(step_actions, ["start_joint_step", "complete_joint_step"])


if __name__ == "__main__":
    unittest.main()
