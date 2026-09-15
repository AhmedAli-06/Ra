"""Vision layer: robust JSON extraction + action parsing (no network, no
screenshot hardware needed)."""
import pytest

from ra.vision import _first_json, _parse_action


def test_fenced_json_action():
    raw = '```json\n{"action": "click", "x": 120, "y": 340, "value": null, "note": "File menu"}\n```'
    step = _parse_action(raw)
    assert step["action"] == "click"
    assert step["x"] == 120 and step["y"] == 340
    assert step["note"] == "File menu"


def test_narration_wrapped_json():
    raw = ('Sure, I can do that.\n'
           '{"action": "type", "x": 10, "y": 55, "value": "hello.txt", "note": "name it"}'
           '\nDone.')
    step = _parse_action(raw)
    assert step["action"] == "type"
    assert step["value"] == "hello.txt"
    assert step["x"] == 10 and step["y"] == 55


def test_only_or_bare_json():
    assert _parse_action('{"action":"done","note":"saved"}')["action"] == "done"
    assert _parse_action('{"action":"scroll","value":-3}')["value"] == -3


def test_unknown_action_becomes_ask():
    step = _parse_action('{"action":"wiggle","note":"???"}')
    assert step["action"] == "ask"


def test_garbage_becomes_ask():
    step = _parse_action("I have no idea what to do here")
    assert step["action"] == "ask"
    assert "Unclear" in step["note"]


def test_first_json_falls_back_to_key_value_hunt():
    data = _first_json('action: "click" x: 42 y: 77 value: null note: "save button"')
    assert data.get("action") == "click"
    assert data.get("y") == 77.0


def test_null_value_and_missing_coords():
    step = _parse_action('{"action":"click","x":400,"y":800,"value":null,"note":"ok"}')
    assert step["value"] is None
    assert step["x"] == 400

    step = _parse_action('{"action":"click","note":"no coords"}')
    assert step["x"] is None and step["y"] is None


def test_scroll_parse_coerces_value_to_int():
    step = _parse_action('{"action":"scroll","value":"5","note":"down"}')
    assert step["value"] == 5