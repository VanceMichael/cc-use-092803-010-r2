import json
import sys

from .auth import Principal
from .clock import parse_time
from .models import Assignment, Event, SupplyLot, Team
from .service import EmergencyPlatform


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        return 2
    item = json.loads(raw)
    platform = EmergencyPlatform(item.get("db", ":memory:"))
    principal = Principal(str(item.get("actor", "operator")), frozenset(item.get("roles", ["commander"])), frozenset(item.get("regions", ["global"])))
    action = item.get("action")
    request_id = str(item.get("request_id", "req"))

    if action == "create_event":
        event = Event(str(item["event_id"]), str(item["region"]), str(item.get("kind", "rain")),
                      int(item.get("severity", 1)), parse_time(item["occurred_at"]), str(item.get("source", "manual")))
        result = platform.create_event(principal, event, request_id)
    elif action == "register_team":
        result = platform.teams.register(
            Team(str(item["team_id"]), str(item["region"]), set(item.get("skills", [])), int(item["capacity"])), request_id)
        platform._persist()
        result = {"team_id": result.team_id, "capacity": result.capacity}
    elif action == "add_supply":
        result = platform.supplies.add(
            SupplyLot(str(item["lot_id"]), str(item["item"]), int(item["quantity"])), request_id)
        platform._persist()
        result = {"lot_id": result.lot_id, "quantity": result.quantity}
    elif action == "assign":
        result = platform.assign(
            principal, Assignment(str(item["assignment_id"]), str(item["event_id"]), str(item["team_id"]),
                                  quantity=int(item.get("quantity", 1))), request_id)
    elif action == "create_joint_plan":
        result = platform.create_joint_plan(principal, str(item["plan_id"]), str(item["region"]), item["steps"], request_id)
    elif action == "start_joint_plan":
        result = platform.start_joint_plan(principal, str(item["plan_id"]), request_id)
    elif action == "pause_joint_plan":
        result = platform.pause_joint_plan(principal, str(item["plan_id"]), request_id)
    elif action == "confirm_step":
        result = platform.confirm_step(principal, str(item["plan_id"]), str(item["step_id"]), request_id)
    elif action == "complete_step":
        result = platform.complete_step(principal, str(item["plan_id"]), str(item["step_id"]), request_id)
    elif action == "cancel_step":
        result = platform.cancel_step(principal, str(item["plan_id"]), str(item["step_id"]), request_id)
    elif action == "reschedule":
        result = platform.reschedule(principal, str(item["plan_id"]), item.get("updates", {}), item.get("new_steps", []), request_id)
    elif action == "view_joint_plan":
        result = platform.joint_plan_view(principal, str(item["plan_id"]))
    else:
        result = {"error": "unsupported action"}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
