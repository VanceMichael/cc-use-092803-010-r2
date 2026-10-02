import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

class StateStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, result TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS checkpoints (name TEXT PRIMARY KEY, sequence INTEGER NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS joint_plans ("
            "plan_id TEXT PRIMARY KEY, region TEXT NOT NULL, state TEXT NOT NULL, "
            "event_ids TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS joint_steps ("
            "plan_id TEXT NOT NULL, step_id TEXT NOT NULL, title TEXT NOT NULL, "
            "priority INTEGER NOT NULL, event_id TEXT, assignment_id TEXT, team_id TEXT, "
            "crew INTEGER NOT NULL, dependencies TEXT NOT NULL, state TEXT NOT NULL, hold_reason TEXT, "
            "blocked_by TEXT NOT NULL, resources TEXT NOT NULL, updated_at TEXT, "
            "PRIMARY KEY (plan_id, step_id))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS joint_step_lots ("
            "plan_id TEXT NOT NULL, step_id TEXT NOT NULL, lot_id TEXT NOT NULL, quantity INTEGER NOT NULL, "
            "PRIMARY KEY (plan_id, step_id, lot_id))"
        )
        self.db.commit()

    def execute(self, sql, params=()):
        cursor = self.db.execute(sql, params)
        self.db.commit()
        return cursor

    def query(self, sql, params=()):
        return self.db.execute(sql, params).fetchall()

    def get_request(self, request_id):
        row = self.db.execute("SELECT result FROM requests WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_request(self, request_id, result):
        self.db.execute("INSERT OR IGNORE INTO requests(request_id,result) VALUES(?,?)", (request_id, json.dumps(result, ensure_ascii=False)))
        self.db.commit()
        return self.get_request(request_id)

    def save_checkpoint(self, name, sequence):
        self.db.execute("INSERT INTO checkpoints(name,sequence) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET sequence=excluded.sequence", (name, sequence))
        self.db.commit()

    def load_checkpoint(self, name):
        row = self.db.execute("SELECT sequence FROM checkpoints WHERE name=?", (name,)).fetchone()
        return int(row[0]) if row else 0

    def close(self):
        self.db.close()
