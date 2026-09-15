"""
Ra Task List (Planner)
=======================
A tiny persistent planner in the spirit of rishaadj/JARVIS's
Planner -> Executor loop. Ra can add tasks or multi-step plans, list them,
mark steps done, and clear work - all stored in a plain JSON file under the
RA data dir so plans survive restarts. No database, stdlib only.
"""
import json
import os
import time
import uuid

from ra import config

_PLAN_FILE = os.path.join(config.DATA_DIR, "planner.json")
_MAX = 200


def _read() -> list:
    if not os.path.exists(_PLAN_FILE):
        return []
    try:
        with open(_PLAN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write(items: list):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def add_task(task: str, steps: list | None = None) -> str:
    """Add one task - or a whole plan (steps) - to the persistent list."""
    task = str(task or "").strip()
    steps = [str(s).strip() for s in (steps or []) if str(s).strip()]
    if not task and not steps:
        return "Nothing to plan."
    _items = _read()
    if len(_items) >= _MAX:
        return "Task list is full - clear a few first."
    entry = {
        "id": uuid.uuid4().hex[:8],
        "task": task[:240],
        "steps": steps[:20],
        "status": "todo",
        "created": int(time.time()),
    }
    _items.append(entry)
    _write(_items)
    n = len(entry["steps"])
    if n:
        return f"Plan added with {n + 1} items."
    return f"Task added: {task[:80]}"


def list_tasks(show_done: bool = False) -> str:
    """Readable, numbered task/plan list (todo/doing/done)."""
    items = _read()
    if not items:
        return "No tasks on the list."
    lines = []
    for i, e in enumerate(items, 1):
        if e.get("status") == "done" and not show_done:
            continue
        mark = {"todo": "[ ]", "doing": "[~]", "done": "[x]"}.get(e.get("status", "todo"), "[ ]")
        head = f"{i}. {mark} {e.get('task') or e.get('id', '?')}"
        lines.append(head)
        for s in (e.get("steps") or []):
            sub = "  - " + (s[:100])
            lines.append(sub)
    return "\n".join(lines) if lines else "All tasks are done."


def complete_task(number: int | None = None, task_id: str | None = None) -> str:
    """Mark a task (by 1-based position or id) as done."""
    items = _read()
    target = None
    if task_id:
        target = next((e for e in items if e.get("id") == str(task_id)), None)
    elif number is not None:
        try:
            n = int(number)
            if 1 <= n <= len(items):
                target = items[n - 1]
        except (TypeError, ValueError):
            return "Give a task number from list_tasks."
    if target is None:
        return "No task found at that position."
    target["status"] = "done"
    _write(items)
    return f"Done: {(target.get('task') or target.get('id'))[:80]}."


def update_status(number: int | None = None, task_id: str | None = None,
                  status: str = "doing") -> str:
    """Flip a task to todo/doing/done."""
    if status not in ("todo", "doing", "done"):
        return "Status must be todo, doing or done."
    items = _read()
    target = None
    if task_id:
        target = next((e for e in items if e.get("id") == str(task_id)), None)
    elif number is not None:
        try:
            n = int(number)
            if 1 <= n <= len(items):
                target = items[n - 1]
        except (TypeError, ValueError):
            return "Give a task number."
    if target is None:
        return "No task found at that position."
    target["status"] = status
    _write(items)
    return f"Task set to {status}."


def remove_task(number: int | None = None, task_id: str | None = None) -> str:
    items = _read()
    target_i = None
    if task_id:
        target_i = next((i for i, e in enumerate(items) if e.get("id") == str(task_id)), None)
    elif number is not None:
        try:
            n = int(number)
            if 1 <= n <= len(items):
                target_i = n - 1
        except (TypeError, ValueError):
            return "Give a task number."
    if target_i is None:
        return "No task found at that position."
    gone = items.pop(target_i)
    _write(items)
    return f"Removed: {(gone.get('task') or gone.get('id'))[:80]}."


def clear_tasks() -> str:
    _write([])
    return "Cleared the task list."