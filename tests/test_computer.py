"""Computer-control engine tests (offline-safe: no clicks/keys injected)."""
import pytest

from ra import config
from ra.computer import computer_access, run_shell, write_file, read_file, set_computer_access
from ra.skills import execute_tool


@pytest.fixture(autouse=True)
def _computer_granted():
    config.GRANTED_ACCESS["computer"] = True
    set_computer_access(True)
    yield
    set_computer_access(True)


def test_computer_access_defaults_on():
    assert computer_access() is True


def test_set_computer_access_toggle():
    set_computer_access(False)
    assert computer_access() is False
    set_computer_access(True)
    assert computer_access() is True


def test_run_shell_echo():
    result = run_shell("echo ra-online")
    assert "ra-online" in result
    assert result.startswith("exit 0")


def test_run_shell_denied_when_disabled():
    set_computer_access(False)
    with pytest.raises(PermissionError):
        run_shell("echo nope")


def test_file_roundtrip(tmp_path):
    path = tmp_path / "sub" / "note.txt"
    assert "Wrote" in write_file(str(path), "hello ra")
    assert read_file(str(path)) == "hello ra"


def test_get_screen_size_shape():
    import re
    from ra.computer import get_screen_size
    assert re.fullmatch(r"\d{2,5}x\d{2,5}", get_screen_size())


def test_skill_dispatch_file_roundtrip(tmp_path):
    path = tmp_path / "skill.txt"
    out = execute_tool("write_file", {"path": str(path), "text": "typed by ra"})
    assert "Wrote" in out
    out = execute_tool("read_file", {"path": str(path)})
    assert "typed by ra" in out


def test_skill_dispatch_computer_present():
    from ra.skills import TOOLS
    names = {t["name"] for t in TOOLS}
    assert {"run_command", "click_screen", "get_clipboard", "focus_window"} <= names