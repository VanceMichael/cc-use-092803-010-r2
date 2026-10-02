import json
import sqlite3
from pathlib import Path

def _dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str)

class StateStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, result TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS checkpoints (name TEXT PRIMARY KEY, sequence INTEGER NOT NULL)")
        self._create_domain_tables()
        self.db.commit()

    def _create_domain_tables(self):
        self.db.execute("""CREATE TABLE IF NOT EXISTS audit_log(
            sequence INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity TEXT NOT NULL, entity_id TEXT NOT NULL, request_id TEXT NOT NULL,
            detail TEXT NOT NULL, created_at TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS events(
            event_id TEXT PRIMARY KEY, region TEXT NOT NULL, kind TEXT NOT NULL,
            severity INTEGER NOT NULL, occurred_at TEXT NOT NULL, source TEXT NOT NULL,
            status TEXT NOT NULL, version INTEGER NOT NULL, metadata TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS teams(
            team_id TEXT PRIMARY KEY, region TEXT NOT NULL, skills TEXT NOT NULL,
            capacity INTEGER NOT NULL, active INTEGER NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS lots(
            lot_id TEXT PRIMARY KEY, item TEXT NOT NULL, quantity INTEGER NOT NULL,
            reserved INTEGER NOT NULL, frozen INTEGER NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS assignments(
            assignment_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, team_id TEXT NOT NULL,
            state TEXT NOT NULL, quantity INTEGER NOT NULL, updated_at TEXT,
            plan_id TEXT, step_id TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS joint_plans(
            plan_id TEXT PRIMARY KEY, region TEXT NOT NULL, state TEXT NOT NULL,
            created_at TEXT, updated_at TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS plan_steps(
            plan_id TEXT NOT NULL, step_id TEXT NOT NULL, event_id TEXT NOT NULL,
            assignment_id TEXT NOT NULL, team_id TEXT NOT NULL, priority INTEGER NOT NULL,
            state TEXT NOT NULL, blocked_by TEXT NOT NULL, waiting_reason TEXT NOT NULL,
            resume_from TEXT NOT NULL, attempts INTEGER NOT NULL, confirmed_request TEXT,
            updated_at TEXT, PRIMARY KEY(plan_id, step_id))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS plan_step_deps(
            plan_id TEXT NOT NULL, step_id TEXT NOT NULL, depends_on TEXT NOT NULL,
            PRIMARY KEY(plan_id, step_id, depends_on))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS plan_step_supplies(
            plan_id TEXT NOT NULL, step_id TEXT NOT NULL, lot_id TEXT NOT NULL,
            quantity INTEGER NOT NULL, PRIMARY KEY(plan_id, step_id, lot_id))""")

    # ---- 幂等请求缓存 ----
    def get_request(self, request_id):
        row = self.db.execute("SELECT result FROM requests WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_request(self, request_id, result):
        self.db.execute("INSERT OR IGNORE INTO requests(request_id,result) VALUES(?,?)", (request_id, _dumps(result)))
        self.db.commit()
        return self.get_request(request_id)

    # ---- 检查点 ----
    def save_checkpoint(self, name, sequence):
        self.db.execute("INSERT INTO checkpoints(name,sequence) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET sequence=excluded.sequence", (name, sequence))
        self.db.commit()

    def load_checkpoint(self, name):
        row = self.db.execute("SELECT sequence FROM checkpoints WHERE name=?", (name,)).fetchone()
        return int(row[0]) if row else 0

    # ---- 审计（增量镜像，序号保持不变） ----
    def insert_audit(self, entry):
        self.db.execute(
            "INSERT OR IGNORE INTO audit_log(sequence,actor,action,entity,entity_id,request_id,detail,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (entry.sequence, entry.actor, entry.action, entry.entity, entry.entity_id,
             entry.request_id, _dumps(entry.detail), entry.created_at.isoformat()))
        self.db.commit()

    def load_audit(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM audit_log ORDER BY sequence")]

    # ---- 领域快照 ----
    def snapshot(self, state):
        """以全量替换方式落盘领域状态；审计与幂等缓存不受影响。"""
        db = self.db
        db.execute("DELETE FROM plan_step_supplies")
        db.execute("DELETE FROM plan_step_deps")
        db.execute("DELETE FROM plan_steps")
        db.execute("DELETE FROM joint_plans")
        db.execute("DELETE FROM assignments")
        db.execute("DELETE FROM lots")
        db.execute("DELETE FROM teams")
        db.execute("DELETE FROM events")
        for event in state["events"]:
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)", (
                event.event_id, event.region, event.kind, event.severity,
                event.occurred_at.isoformat(), event.source, event.status,
                event.version, _dumps(event.metadata)))
        for team in state["teams"]:
            db.execute("INSERT INTO teams VALUES(?,?,?,?,?)", (
                team.team_id, team.region, _dumps(sorted(team.skills)),
                team.capacity, 1 if team.active else 0))
        for lot in state["lots"]:
            db.execute("INSERT INTO lots VALUES(?,?,?,?,?)", (
                lot.lot_id, lot.item, lot.quantity, lot.reserved, 1 if lot.frozen else 0))
        for item in state["assignments"]:
            db.execute("INSERT INTO assignments VALUES(?,?,?,?,?,?,?,?)", (
                item.assignment_id, item.event_id, item.team_id, item.state,
                item.quantity, item.updated_at.isoformat() if item.updated_at else None,
                item.plan_id, item.step_id))
        for plan, steps in state["plans"]:
            db.execute("INSERT INTO joint_plans VALUES(?,?,?,?,?)", (
                plan.plan_id, plan.region, plan.state,
                plan.created_at.isoformat() if plan.created_at else None,
                plan.updated_at.isoformat() if plan.updated_at else None))
            for step in steps:
                db.execute("INSERT INTO plan_steps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    plan.plan_id, step.step_id, step.event_id, step.assignment_id,
                    step.team_id, step.priority, step.state, _dumps(step.blocked_by),
                    step.waiting_reason, step.resume_from, step.attempts,
                    step.confirmed_request,
                    step.updated_at.isoformat() if step.updated_at else None))
                for dep in step.dependencies:
                    db.execute("INSERT INTO plan_step_deps VALUES(?,?,?)", (plan.plan_id, step.step_id, dep))
                for use in step.supplies:
                    db.execute("INSERT INTO plan_step_supplies VALUES(?,?,?,?)", (plan.plan_id, step.step_id, use.lot_id, use.quantity))
        self.db.commit()

    def load_snapshot(self):
        rows = {
            "events": [dict(row) for row in self.db.execute("SELECT * FROM events")],
            "teams": [dict(row) for row in self.db.execute("SELECT * FROM teams")],
            "lots": [dict(row) for row in self.db.execute("SELECT * FROM lots")],
            "assignments": [dict(row) for row in self.db.execute("SELECT * FROM assignments")],
            "plans": [dict(row) for row in self.db.execute("SELECT * FROM joint_plans")],
            "steps": [dict(row) for row in self.db.execute("SELECT * FROM plan_steps")],
            "deps": [dict(row) for row in self.db.execute("SELECT * FROM plan_step_deps")],
            "supplies": [dict(row) for row in self.db.execute("SELECT * FROM plan_step_supplies")],
        }
        return rows

    def close(self):
        self.db.close()
