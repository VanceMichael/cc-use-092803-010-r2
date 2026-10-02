import json
import sys
from .auth import Principal
from .models import Assignment, Event, SupplyLot, Team
from .service import EmergencyPlatform
from .joint_plans import plan_from_dict, step_from_dict


def _platform(item):
    return EmergencyPlatform(item.get("db", ":memory:"))


def _principal(item):
    return Principal(
        str(item.get("actor", "operator")),
        frozenset(item.get("roles", ["commander"])),
        frozenset(item.get("regions", ["global"])),
    )


def dispatch(platform, principal, item):
    action = item.get("action")

    if action == "create_event":
        from .clock import parse_time
        event = Event(str(item["event_id"]), str(item["region"]), str(item.get("kind", "rain")),
                      int(item.get("severity", 1)), parse_time(item["occurred_at"]),
                      str(item.get("source", "manual")))
        return platform.create_event(principal, event, str(item["request_id"]))

    if action == "register_team":
        team = Team(str(item["team_id"]), str(item["region"]), set(item.get("skills", [])),
                    int(item.get("capacity", 1)))
        result = platform.teams.register(team, str(item["request_id"]))
        return {"team_id": result.team_id, "capacity": result.capacity}

    if action == "add_supply":
        lot = SupplyLot(str(item["lot_id"]), str(item["item"]), int(item.get("quantity", 0)))
        result = platform.supplies.add(lot, str(item["request_id"]))
        return {"lot_id": result.lot_id, "quantity": result.quantity, "reserved": result.reserved}

    if action == "assign":
        assignment = Assignment(str(item["assignment_id"]), str(item["event_id"]),
                                str(item["team_id"]), quantity=int(item.get("quantity", 0)))
        return platform.assign(principal, assignment, str(item["request_id"]))

    if action == "create_joint_plan":
        return platform.create_joint_plan(principal, plan_from_dict(item["plan"]), str(item["request_id"]))
    if action == "start_joint_plan":
        return platform.start_joint_plan(principal, str(item["plan_id"]), str(item["request_id"]))
    if action == "pause_joint_plan":
        return platform.pause_joint_plan(principal, str(item["plan_id"]), str(item["request_id"]))
    if action == "start_joint_step":
        return platform.start_joint_step(principal, str(item["plan_id"]), str(item["step_id"]),
                                         str(item["request_id"]))
    if action == "confirm_joint_step":
        return platform.confirm_joint_step(principal, str(item["plan_id"]), str(item["step_id"]),
                                           str(item["request_id"]))
    if action == "cancel_joint_step":
        return platform.cancel_joint_step(principal, str(item["plan_id"]), str(item["step_id"]),
                                          str(item["request_id"]))
    if action == "reschedule_joint_plan":
        updated = [step_from_dict(s) for s in item.get("steps", [])]
        return platform.reschedule_joint_plan(principal, str(item["plan_id"]), updated,
                                              str(item["request_id"]))
    if action == "get_joint_plan":
        return platform.get_joint_plan(principal, str(item["plan_id"]))
    if action == "recover_joint_plans":
        return {"plans": platform.recover_joint_plans(principal)}

    return {"error": "unsupported action"}


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        return 2
    item = json.loads(raw)
    platform = _platform(item)
    principal = _principal(item)
    result = dispatch(platform, principal, item)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
