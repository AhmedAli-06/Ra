"""
Ra Plugin Directory
====================
InterGenJLU/jarvis-style one-file plugin system: any `.py` file the user drops
into `~/.ra/plugins/` that exposes a `register()` function becomes a new skill
Ra can call. No config file, no CLI, no pip install.

`register()` must return a list/tuple of tool dicts:
    {
        "name": "my_tool",
        "description": "one line for the LLM",
        "input_schema": {"type": "object", "properties": {...}},
        "handler": callable(args: dict) -> str,
    }

Handlers are run in Ra's process on demand - they have full Python (and can
import ra.skills / ra.computer for screen, keys, files, etc.).
"""
import importlib.util
import os
import sys
import traceback

from ra import config
from ra import logging as ralog


def plugin_dir() -> str:
    d = os.path.join(config.DATA_DIR, "plugins")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def discover(limit=200) -> tuple:
    """Scan the plugin dir for *.py files with a callable `register()`.
    Returns (plugins, errors) where each plugin is a dict with keys
    'module', 'path' and the raw 'tools' list from register()."""
    d = plugin_dir()
    plugins, errors = [], []
    if not os.path.isdir(d):
        return plugins, errors
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".py") and not n.startswith("_"))
    except OSError as e:
        return plugins, [f"plugin dir {d}: {e}"]
    for i, name in enumerate(names[:limit]):
        path = os.path.join(d, name)
        mod_name = f"ra_user_plugin_{i}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                errors.append(f"{name}: cannot load")
                continue
            mod = importlib.util.module_from_spec(spec)
            # The plugin's handlers may import ra.skills / ra.computer.
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            register = getattr(mod, "register", None)
            if not callable(register):
                errors.append(f"{name}: no register() function")
                continue
            tools = register()
            if not isinstance(tools, (list, tuple)):
                errors.append(f"{name}: register() must return a list of tool dicts")
                continue
            plugins.append({"module": mod_name, "path": path, "name": name, "tools": list(tools)})
        except Exception as e:
            errors.append(f"{name}: {e}")
            ralog.log("err", f"plugin {name} failed to load: {e}\n{traceback.format_exc(limit=2)}")
    return plugins, errors


def _is_valid_tool(t: dict) -> bool:
    return bool(
        isinstance(t, dict)
        and t.get("name")
        and t.get("description")
        and callable(t.get("handler"))
    )


def install(tools_list: list, dispatch: dict) -> tuple:
    """Merge discovered plugins into Ra's TOOLS list and _DISPATCH dict.
    Idempotent: a tool whose name already exists is skipped. Returns
    (installed_names, errors)."""
    plugins, errors = discover()
    installed = []
    for p in plugins:
        for t in p["tools"]:
            if not _is_valid_tool(t):
                errors.append(f"{p['name']}: tool missing name/description/handler")
                continue
            name = t["name"]
            if any(x.get("name") == name for x in tools_list):
                continue
            tools_list.append({
                "name": name,
                "description": t["description"],
                "input_schema": t.get("input_schema") or {"type": "object", "properties": {}},
            })
            dispatch[name] = t["handler"]
            installed.append(name)
            ralog.log("ok", f"plugin tool installed: {name} (from {p['name']})")
    return installed, errors


def list_plugins() -> str:
    """Readable report of installed plugin tools, for the LLM-facing tool."""
    from ra import skills
    PluginNames = [t["name"] for t in skills.TOOLS]
    return "\n".join(PluginNames) if PluginNames else "No user plugins installed."