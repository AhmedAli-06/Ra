"""Fast-lane: deterministic instant answers for well-formed trivial queries,
plus the persistence tools they route to (timers, memory, news)."""
import pytest

from ra import brain, fastlane


def test_time_instant():
    assert fastlane.try_answer("what time is it").startswith("It's")
    assert fastlane.try_answer("whats the time").startswith("It's")
    assert fastlane.try_answer("what day is it").startswith("It's")


def test_time_with_location_falls_through():
    assert fastlane.try_answer("what time is it in tokyo") is None
    assert fastlane.try_answer("what time will it be in 3 hours") is None


def test_battery_instant():
    r = fastlane.try_answer("how is the battery")
    assert r is None or "battery" in r.lower() or "Battery" in r


def test_explanations_fall_through():
    assert fastlane.try_answer("explain how a battery works") is None
    assert fastlane.try_answer("what is a battery") is None


def test_timer_fires():
    r = fastlane.try_answer("set a timer for 5 minutes for pasta")
    assert r == "Timer set for 300 seconds. Reminder: pasta."


def test_reminder_fires():
    r = fastlane.try_answer("remind me in 10 minutes to drink water")
    assert "600" in r and "water" in r


def test_fast_lane_short_circuits_llm():
    """brain.ask must answer a fast-lane query without ever reaching the
    (missing/offline) model client."""
    fake = object()  # any non-client object would crash _converse if reached
    reply = brain.ask("what time is it", client=fake, retrieve=False)
    assert reply.startswith("It's")
    assert brain._history[-1]["role"] == "assistant"


def test_fast_lane_non_match_reaches_llm():
    from types import SimpleNamespace
    from ra import config

    def _reply_message(content=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))]
        )

    class FakeCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kw):
            self.calls.append(kw)
            return _reply_message("ok")

    class FakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch_place = pytest.MonkeyPatch()
    monkeypatch_place.setattr(config, "RAG_ENABLED", False)
    fake = FakeClient()
    brain.reset_history()
    reply = brain.ask("tell me a story", client=fake, retrieve=False)
    assert reply == "ok"
    assert len(fake.chat.completions.calls) == 1
    monkeypatch_place.undo()