"""Multi-brain race tests: three configured brains (gemini/groq/nim) receive the
same turn in parallel, the FIRST valid answer wins, and one-or-two-down never
stops the others. All clients are fakes - never any network."""
import time
from types import SimpleNamespace

import pytest

from ra import brain


def _reply_message(content, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=content, tool_calls=tool_calls or []))]
    )


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id,
                           function=SimpleNamespace(name=name, arguments=arguments))


class FakeCompletions:
    def __init__(self, script=None, delay=0.0, error=None):
        self.script = list(script or [])
        self.calls = []
        self.delay = delay
        self.error = error

    def create(self, **kw):
        self.calls.append(kw)
        if self.error is not None:
            raise self.error
        if self.delay:
            time.sleep(self.delay)
        step = self.script.pop(0) if self.script else {"content": "Acknowledged."}
        if "tool" in step:
            return _reply_message(None, [_tool_call(
                f"call_{len(self.calls)}", step["tool"][0], step["tool"][1])])
        return _reply_message(step.get("content", "Acknowledged."))


class FakeClient:
    def __init__(self, script=None, delay=0.0, error=None):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(script, delay=delay, error=error))


@pytest.fixture(autouse=True)
def _reset_race_state():
    brain._provider_failures.clear()
    brain._provider_clients.clear()
    brain._last_winner_provider = None
    brain._healed_models.clear()
    brain._dead_models.clear()
    brain.reset_history()
    yield
    brain._provider_failures.clear()
    brain._provider_clients.clear()
    brain._last_winner_provider = None
    brain._healed_models.clear()
    brain._dead_models.clear()
    brain.reset_history()


@pytest.fixture()
def multi_env(monkeypatch):
    """Turn the 3-brain race ON and pretend three keys exist (groq explicitly
    opted in - it is off by default since free-tier can't fit the payload)."""
    monkeypatch.setenv("RA_MULTI_BRAIN", "1")
    monkeypatch.setenv("RA_GEMINI_API_KEY", "test-gem")
    monkeypatch.setenv("RA_GROQ_API_KEY", "test-groq")
    monkeypatch.setenv("RA_GROQ_ENABLE", "1")
    monkeypatch.setenv("RA_GROQ_BASE_URL", "https://groq.test/v1")
    monkeypatch.setenv("RA_NIM_API_KEY", "test-nim")
    monkeypatch.setenv("RA_NIM_BASE_URL", "https://nim.test/v1")
    return True


@pytest.fixture()
def install_fakes(monkeypatch):
    """Route every provider's client creation to per-provider FakeClients."""
    holders = {}

    def _install(fakes):
        holders["fakes"] = fakes

        def make_client(provider):
            return fakes[provider]

        monkeypatch.setattr(brain, "_make_client", make_client)
        return fakes

    _install.h = holders
    return _install


def _assert_no_empty_names(messages):
    for m in messages:
        for c in (m.get("tool_calls") or []):
            assert c["function"]["name"].strip()
        if m["role"] == "tool":
            assert m["tool_call_id"]


def test_fastest_brain_wins(multi_env, install_fakes):
    fakes = install_fakes({
        "gemini": FakeClient([{"content": "gem answer"}], delay=0.3),
        "groq": FakeClient([{"content": "groq answer"}], delay=0.05),
        "nim": FakeClient([{"content": "nim answer"}], delay=0.6),
    })
    reply = brain.ask("hi", retrieve=False)
    assert reply == "groq answer"  # fastest, not the most accurate
    # every brain saw the same turn (the race), and only once
    for p in ("gemini", "groq", "nim"):
        assert len(fakes[p].chat.completions.calls) == 1
    # history holds only ONE assistant reply (losers were discarded)
    assistants = [m for m in brain._history if m["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == "groq answer"


def test_one_brain_down_survivors_carry_on(multi_env, install_fakes):
    class Boom(Exception):
        pass

    fakes = install_fakes({
        "gemini": FakeClient(error=Boom("gemini is down")),  # fails instantly
        "groq": FakeClient([{"content": "groq answer"}], delay=0.05),
        "nim": FakeClient([{"content": "nim answer"}], delay=0.2),
    })
    reply = brain.ask("hi", retrieve=False)
    assert reply == "groq answer"  # the first healthy brain that finished
    assert "gemini" in brain._provider_failures  # it was parked, not crashing
    assert brain._last_winner_provider == "groq"


def test_two_brains_down_one_answer_still_survives(multi_env, install_fakes):
    class Boom(Exception):
        pass

    fakes = install_fakes({
        "gemini": FakeClient(error=Boom("down")),
        "groq": FakeClient(error=Boom("down")),
        "nim": FakeClient([{"content": "nim answer"}], delay=0.05),
    })
    reply = brain.ask("hi", retrieve=False)
    assert reply == "nim answer"
    assert brain._last_winner_provider == "nim"
    assert set(brain._provider_failures) == {"gemini", "groq"}


def test_all_brains_down_raises_clear_error(multi_env, install_fakes):
    class Boom(Exception):
        pass

    install_fakes({
        "gemini": FakeClient(error=Boom("down")),
        "groq": FakeClient(error=Boom("down")),
        "nim": FakeClient(error=Boom("down")),
    })
    with pytest.raises(RuntimeError) as ei:
        brain.ask("hi", retrieve=False)
    assert "brains failed" in str(ei.value)


def test_empty_answer_counts_as_loss(multi_env, install_fakes):
    """A 200 with empty content is useless - it must not win the race."""
    fakes = install_fakes({
        "gemini": FakeClient([{"content": ""}], delay=0.3),
        "groq": FakeClient([{"content": "the real answer"}]),
        "nim": FakeClient([{"content": ""}], delay=0.6),
    })
    reply = brain.ask("hi", retrieve=False)
    assert reply == "the real answer"
    assert brain._last_winner_provider == "groq"


def test_tool_loop_executes_on_winner_only_once(multi_env, install_fakes, monkeypatch):
    executed = []

    def fake_exec_one(tc):
        executed.append(tc.function.name)
        return {}, "tool ran"

    monkeypatch.setattr(brain, "_exec_one", fake_exec_one)
    fakes = install_fakes({
        # both groq and nim try to call a tool (same shape); only the winner runs.
        "gemini": FakeClient([{"content": "gem answer"}], delay=0.4),
        "groq": FakeClient([{"tool": ("get_time", "{}")},
                            {"content": "done via groq"}]),
        "nim": FakeClient([{"tool": ("get_time", "{}")},
                           {"content": "done via nim"}], delay=0.2),
    })
    reply = brain.ask("tell me the time via a tool if you can", retrieve=False)
    assert reply == "done via groq"  # groq won the race
    assert len(executed) == 1  # tools ran EXACTLY once - only on the winner
    assert [m["role"] for m in brain._history].count("tool") == 1
    _assert_no_empty_names(brain._history)


def test_streamed_winner_content_reaches_on_token(multi_env, install_fakes):
    captured = []
    fakes = install_fakes({
        "gemini": FakeClient([{"content": "gem answer"}], delay=0.3),
        "groq": FakeClient([{"content": "streamed groq answer"}], delay=0.05),
        "nim": FakeClient([{"content": "nim answer"}], delay=0.6),
    })
    reply = brain.ask("hi", retrieve=False, stream=True, on_token=captured.append)
    assert reply == "streamed groq answer"
    assert "".join(captured) == "streamed groq answer"  # winner replayed to HUD


def test_explicit_off_runs_single_brain(install_fakes):
    """RA_MULTI_BRAIN=0 must fall back to the plain single-brain path - the
    user's own injected client is used and the race never spawns."""
    install_fakes({
        "gemini": FakeClient([{"content": "gem answer"}]),
        "groq": FakeClient([{"content": "groq answer"}]),
        "nim": FakeClient([{"content": "nim answer"}]),
    })
    fake = FakeClient([{"content": "single brain answer"}])
    reply = brain.ask("hi", client=fake, retrieve=False)
    assert reply == "single brain answer"
    assert len(fake.chat.completions.calls) == 1


def test_cooldown_expiry_lets_brain_back_in(multi_env, install_fakes):
    """A parked brain is excluded from the race until its cooldown expires,
    then it races again - no permanent exile, no crash."""
    brain._provider_failures["gemini"] = time.monotonic() + 60  # parked
    assert not brain._provider_ok("gemini")
    brain._provider_failures["gemini"] = time.monotonic() - 1   # expired
    assert brain._provider_ok("gemini")
    fakes = install_fakes({
        "gemini": FakeClient([{"content": "back online"}]),
        "groq": FakeClient([{"content": "still fast"}], delay=0.1),
        "nim": FakeClient([{"content": "nim online"}], delay=0.1),
    })
    reply = brain.ask("hello there", retrieve=False)
    assert reply in ("back online", "still fast", "nim online")
    assert len(fakes["gemini"].chat.completions.calls) >= 1


def test_dead_model_parks_brain_for_long_cooldown(multi_env, install_fakes):
    """A model the provider no longer hosts (404 'not found for account') or a
    rejected key (401) can NEVER heal on a retry. It must park the brain on the
    LONG cooldown - not the 10s crash cooldown - so it is not re-hit, and not
    re-logged, on every single turn (the 'errors appear all the time' bug:
    NVIDIA decommissioned the configured NIM model and Ra kept calling it)."""
    class NotFound(Exception):
        status_code = 404

    class Unauthorized(Exception):
        status_code = 401

    assert brain._is_dead_config_error(NotFound("not found for account"))
    assert brain._is_dead_config_error(Unauthorized("bad key"))
    assert brain._is_dead_config_error(Exception("Model not found for account 'x'"))
    assert not brain._is_dead_config_error(Exception("connection reset"))
    assert not brain._is_dead_config_error(Exception("boom"))

    install_fakes({
        "gemini": FakeClient([{"content": "gem answer"}]),
        "groq": FakeClient(error=NotFound("Function not found for account")),
        "nim": FakeClient([{"content": "nim answer"}], delay=0.3),
    })
    reply = brain.ask("hi", retrieve=False)
    assert reply in ("gem answer", "nim answer")
    for name, ts in brain._provider_failures.items():
        assert ts > time.monotonic() + 60, f"{name} got a short cooldown"


def test_dead_brain_is_not_retried_on_the_next_turn(multi_env, install_fakes):
    """Once parked, a dead brain receives NO further requests until its
    cooldown expires - so one bad model can't spam failures every turn."""
    class NotFound(Exception):
        status_code = 404

    fakes = install_fakes({
        # Two-step scripts: this test asks TWICE, so each fake must be able to
        # serve both turns (an exhausted script would answer "Acknowledged." and
        # win the race, which is what made this assertion flaky).
        "gemini": FakeClient([{"content": "gem answer"}, {"content": "gem answer"}]),
        "groq": FakeClient(error=NotFound("not found for account")),
        "nim": FakeClient([{"content": "nim answer"}, {"content": "nim answer"}], delay=0.3),
    })
    brain.ask("first", retrieve=False)
    dead_calls = len(fakes["groq"].chat.completions.calls)
    assert "groq" in brain._provider_failures          # parked
    second = brain.ask("second", retrieve=False)
    assert second in ("gem answer", "nim answer")
    assert len(fakes["groq"].chat.completions.calls) == dead_calls  # never re-asked


class _ModelNotFound(Exception):
    """Exactly what a provider returns for a retired/renamed hosted model."""
    status_code = 404

    def __init__(self, msg="Function 'abc': not found for account 'acct'"):
        super().__init__(msg)


class ModelAwareCompletions(FakeCompletions):
    """Completions that 404 for specific model names, like a provider that has
    retired a hosted model for this account."""

    def __init__(self, dead_models, script=None, **kw):
        super().__init__(script, **kw)
        self.dead_models = set(dead_models)

    def create(self, **kw):
        if kw.get("model") in self.dead_models:
            self.calls.append(kw)
            raise _ModelNotFound()
        return super().create(**kw)


def test_missing_model_self_heals_to_a_working_sibling(multi_env, install_fakes,
                                                       monkeypatch):
    """A retired model name must not cost Ra a brain: the turn is retried on a
    sibling model, which is remembered for later turns - instead of the brain
    being parked (and rejected) on every turn for the whole session."""
    monkeypatch.setattr(brain.config, "model_for", lambda provider: "gone-model")
    monkeypatch.setattr(brain.config, "model_candidates",
                        lambda provider: ["gone-model", "good-model"])

    nim = FakeClient()
    nim.chat.completions = ModelAwareCompletions(
        {"gone-model"}, [{"content": "nim answer"}, {"content": "nim answer"}])
    brain._tag_provider(nim, "nim")

    fakes = install_fakes({
        "gemini": FakeClient([{"content": "gem answer"}, {"content": "gem answer"}],
                             delay=0.3),
        "groq": FakeClient(error=_ModelNotFound(), delay=0.05),
        "nim": nim,
    })

    reply = brain.ask("first", retrieve=False)
    assert reply == "nim answer"
    assert brain._healed_models["nim"] == "good-model"   # remembered
    assert "nim" not in brain._provider_failures         # healed, NOT parked

    reply2 = brain.ask("second", retrieve=False)
    assert reply2 == "nim answer"
    tried = [c.get("model") for c in fakes["nim"].chat.completions.calls]
    assert tried.count("gone-model") == 1                # never re-probed
    assert tried.count("good-model") == 2


def test_auth_failure_is_not_treated_as_a_dead_model():
    """A 401 fails every model alike, so Ra must not burn sibling-model calls on
    it; only a model-specific rejection drives the self-heal."""
    class Auth(Exception):
        status_code = 401

    class Missing(Exception):
        status_code = 404

    assert not brain._is_model_missing_error(Auth("invalid api key"))
    assert brain._is_model_missing_error(Missing("not found for account"))
    assert brain._is_model_missing_error(
        Missing("models/gemini-2.5-flash is no longer available to new users"))
    assert not brain._is_model_missing_error(Exception("connection refused"))


def test_groq_excluded_unless_opted_in(monkeypatch, install_fakes):
    """Groq's free tier can't carry Ra's real payload (system+tools ~9.2k
    tokens vs ~8k TPM), so unless RA_GROQ_ENABLE=1 it must NOT race - its
    absence is silent, never an error."""
    monkeypatch.setenv("RA_MULTI_BRAIN", "1")
    monkeypatch.setenv("RA_GEMINI_API_KEY", "test-gem")
    monkeypatch.setenv("RA_GROQ_API_KEY", "test-groq")     # present but not enabled
    monkeypatch.setenv("RA_NIM_API_KEY", "test-nim")
    fakes = install_fakes({
        "gemini": FakeClient([{"content": "gem answer"}]),
        "groq": FakeClient([{"content": "groq answer"}], delay=0.01),
        "nim": FakeClient([{"content": "nim answer"}], delay=0.3),
    })
    reply = brain.ask("hi", retrieve=False)
    assert reply == "gem answer"                       # groq never asked
    assert len(fakes["groq"].chat.completions.calls) == 0
    assert "groq" not in brain._provider_failures
    assert brain._last_winner_provider == "gemini"


def test_quota_error_parks_brain_for_long_cooldown(multi_env, install_fakes):
    """413/429 (quota) must not be retried by the caller - the brain parks
    for the extended 150s quota cooldown, exactly like a dead brain."""
    class RateLimit(Exception):
        pass

    import ra.brain as _brain
    assert _brain._is_quota_error(RateLimit("429 Too Many Requests"))
    assert _brain._is_quota_error(RateLimit("413 Request Entity Too Large"))
    assert _brain._is_quota_error(RateLimit("quota exceeded for tpm"))
    assert not _brain._is_quota_error(RateLimit("connection refused"))

    install_fakes({
        "gemini": FakeClient(error=RateLimit("429 Too Many Requests"), delay=0.2),
        "groq": FakeClient([{"content": "groq answer"}]),
        "nim": FakeClient([{"content": "nim answer"}], delay=0.3),
    })
    reply = brain.ask("hi", retrieve=False)
    assert reply == "groq answer"  # race never re-attacked the capped brain
    # Whatever the scheduling chooses, any parked brain is parked for the LONG
    # quota window (150s), never the short 10s crash cooldown.
    for name, ts in brain._provider_failures.items():
        assert ts > time.monotonic() + 60, f"{name} got a short cooldown"