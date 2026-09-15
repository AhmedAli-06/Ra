"""Plugin directory: one-file skill plugins (InterGenJLU pattern)."""
import os
import textwrap

from ra import plugins


def _write_plugin(d, src: str):
    pd = d / "plugins"
    pd.mkdir(exist_ok=True)
    (pd / "myplugin.py").write_text(textwrap.dedent(src).lstrip(), encoding="utf-8")


def test_discover_empty_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("ra.config.DATA_DIR", str(tmp_path))
    pl, errs = plugins.discover()
    assert pl == []
    assert errs == []


def test_plugin_loads_and_returns_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("ra.config.DATA_DIR", str(tmp_path))
    _write_plugin(tmp_path, """
        def greet(args):
            return f"hi {args.get('who')}"
        def register():
            return [{
                "name": "my_greet",
                "description": "custom greeting",
                "input_schema": {"type": "object", "properties": {"who": {"type": "string"}}},
                "handler": greet,
            }]
    """)
    pl, errs = plugins.discover()
    assert not errs
    assert len(pl) == 1
    assert pl[0]["tools"][0]["name"] == "my_greet"


def test_install_wires_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr("ra.config.DATA_DIR", str(tmp_path))
    _write_plugin(tmp_path, """
        def register():
            return [{
                "name": "power_two",
                "description": "square a number",
                "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "handler": lambda a: str(int(a.get("n", 0)) ** 2),
            }]
    """)
    tools, dispatch = [], {}
    added, errs = plugins.install(tools, dispatch)
    assert not errs
    assert added == ["power_two"]
    assert dispatch["power_two"]({"n": 4}) == "16"
    assert tools[0]["name"] == "power_two"


def test_install_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("ra.config.DATA_DIR", str(tmp_path))
    _write_plugin(tmp_path, """
        def register():
            return [{"name": "dup_tool", "description": "d",
                     "input_schema": {"type": "object", "properties": {}},
                     "handler": lambda a: "ok"}]
    """)
    tools = [{"name": "dup_tool", "description": "builtin", "input_schema": {}}]
    dispatch = {"dup_tool": lambda a: "ok"}
    added, _ = plugins.install(tools, dispatch)
    assert added == []  # does not override a builtin


def test_broken_plugin_reports_error_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr("ra.config.DATA_DIR", str(tmp_path))
    pd = tmp_path / "plugins"
    pd.mkdir(exist_ok=True)
    (pd / "bad.py").write_text("def register():\n    raise RuntimeError('boom')", encoding="utf-8")
    pl, errs = plugins.discover()
    assert pl == []
    assert any("boom" in e for e in errs)


def test_register_returning_scalar_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("ra.config.DATA_DIR", str(tmp_path))
    pd = tmp_path / "plugins"
    pd.mkdir(exist_ok=True)
    (pd / "bad.py").write_text("def register():\n    return 42", encoding="utf-8")
    pl, errs = plugins.discover()
    assert pl == []
    assert any("list" in e for e in errs)


def dedent(s: str) -> str:
    return textwrap.dedent(s)
