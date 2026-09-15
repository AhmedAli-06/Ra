"""Brain behavior tests with an injected fake LLM client (no network, no groq)."""
import json
from types import SimpleNamespace

import pytest

from ra import config
from ra import brain
from ra.rag import indexer
from ra.rag.embedder import HashEmbedder
from ra.rag.store import SemanticStore


def _reply_message(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls or []))]
    )


def _tool_call(call_id, name, arguments, extra_content=None):
    tc = SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))
    if extra_content is not None:
        tc.extra_content = extra_content
    return tc


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        step = self.script.pop(0) if self.script else {"content": "Acknowledged."}
        if "tool" in step:
            name = step["tool"][0]
            args = step["tool"][1]
            extra = step["tool"][2] if len(step["tool"]) > 2 else None
            return _reply_message(None, [_tool_call(f"call_{len(self.calls)}", name, args, extra)])
        return _reply_message(step.get("content", "Acknowledged."))


class FakeClient:
    def __init__(self, script):
        self.chat = SimpleNamespace(completions=FakeCompletions(script))


@pytest.fixture(autouse=True)
def _clean_history():
    brain.reset_history()
    yield
    brain.reset_history()


def test_simple_reply_no_rag_no_tools():
    fake = FakeClient([{"content": "Hello from Ra."}])
    reply = brain.ask("hi", client=fake, retrieve=False)
    assert reply == "Hello from Ra."
    msgs = fake.chat.completions.calls[0]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[-1] == {"role": "user", "content": "hi"}


def test_tool_loop_executes_tool(monkeypatch):
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    script = [
        {"tool": ("get_time", "{}")},
        {"content": "It is 10:30 PM."},
    ]
    fake = FakeClient(script)
    reply = brain.ask("tell me the time", client=fake)
    assert reply == "It is 10:30 PM."
    assert len(fake.chat.completions.calls) == 2
    # history records assistant tool_call + tool result
    roles = [m["role"] for m in brain._history]
    assert roles.count("tool") == 1
    assert roles.count("assistant") == 2


def test_gemini_thought_signature_is_replayed(monkeypatch):
    """Gemini 3.x requires the model-issued thought_signature to be replayed on
    the assistant tool_call entry in the next request (400 otherwise)."""
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    sig = {"google": {"thought_signature": "EoYCCoMCARFN..."}}
    script = [
        {"tool": ("get_time", "{}", sig)},
        {"content": "It is 10:30 PM."},
    ]
    fake = FakeClient(script)
    reply = brain.ask("tell me the time", client=fake)
    assert reply == "It is 10:30 PM."
    second = fake.chat.completions.calls[1]["messages"]
    assistant = [m for m in second if m["role"] == "assistant"][-1]
    assert assistant["tool_calls"][0]["extra_content"] == sig
    # and the request payload carries it back verbatim
    assert fake.chat.completions.calls[1]["messages"] == second


def test_auto_retrieval_injects_context(monkeypatch, tmp_path):
    idx = str(tmp_path / "idx.sqlite")
    store = SemanticStore(idx)
    (tmp_path / "memo.txt").write_text(
        "The budget committee agreed to fund the mountain road repair project "
        "with two million dollars approved today.", encoding="utf-8")
    indexer.index_directory(str(tmp_path), store, HashEmbedder(dim=512))

    monkeypatch.setattr(config, "RAG_INDEX_PATH", idx)
    monkeypatch.setattr(config, "RAG_MIN_SCORE", 0.0)

    fake = FakeClient([{"content": "They approved the mountain road funding."}])
    reply = brain.ask("what did the budget committee decide?", client=fake)
    assert reply
    msgs = fake.chat.completions.calls[0]["messages"]
    assert len(msgs) >= 3  # system prompt + retrieved context + user
    joined = "\n".join(m.get("content", "") for m in msgs)
    assert "budget committee agreed" in joined
    assert "memo.txt" in joined


def test_auto_retrieval_skipped_for_weak_match(monkeypatch, tmp_path):
    idx = str(tmp_path / "idx.sqlite")
    store = SemanticStore(idx)
    (tmp_path / "memo.txt").write_text(
        "The budget committee agreed to fund the mountain road repair project.", encoding="utf-8")
    indexer.index_directory(str(tmp_path), store, HashEmbedder(dim=512))

    monkeypatch.setattr(config, "RAG_INDEX_PATH", idx)
    monkeypatch.setattr(config, "RAG_MIN_SCORE", 0.99)  # nothing will pass

    fake = FakeClient([{"content": "Pasta is delicious."}])
    brain.ask("tell me about pasta cooking", client=fake)
    msgs = fake.chat.completions.calls[0]["messages"]
    assert len(msgs) == 2  # system + user, no context block
    assert not any("budget committee" in m.get("content", "") for m in msgs)


def test_search_context_tool_via_skills(monkeypatch, tmp_path):
    """search_context goes through execute_tool and returns indexed passages."""
    idx = str(tmp_path / "idx.sqlite")
    store = SemanticStore(idx)
    (tmp_path / "wifi.txt").write_text("Home wifi password is sunset-sunrise-42.", encoding="utf-8")
    indexer.index_directory(str(tmp_path), store, HashEmbedder(dim=512))
    monkeypatch.setattr(config, "RAG_INDEX_PATH", idx)

    from ra.skills import execute_tool
    out = execute_tool("search_context", {"query": "wifi password"})
    assert "wifi.txt" in out
    assert "sunset" in out


def _delta_chunk(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls or []))]
    )


def _delta_tc(index, tool_id=None, name="", arguments=""):
    tc = SimpleNamespace(index=index, function=SimpleNamespace(name=name, arguments=arguments))
    if tool_id:
        tc.id = tool_id
    return tc


class FakeStream:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return iter(self.turns.pop(0))


def _assert_no_empty_names(messages):
    for m in messages:
        for c in (m.get("tool_calls") or []):
            assert c["function"]["name"].strip()
        if m["role"] == "tool":
            assert m["tool_call_id"]


def test_streamed_empty_name_tool_call_is_dropped(monkeypatch):
    """A streamed tool-call fragment with arguments but no function name must
    never reach history (Gemini rejects it with a 400, crashing the reply)."""
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    turns = [
        [
            _delta_chunk(tool_calls=[_delta_tc(0, "call_1", name="get_time")]),
            _delta_chunk(tool_calls=[_delta_tc(0, arguments="{}")]),
            _delta_chunk(tool_calls=[_delta_tc(1, "call_9", arguments="{}")]),
        ],
        [_delta_chunk(content="It is 10:30 PM.")],
    ]
    fake = SimpleNamespace(chat=SimpleNamespace(completions=FakeStream(turns)))
    reply = brain.ask("tell me the time", client=fake, retrieve=False, stream=True)
    assert reply == "It is 10:30 PM."
    tool_msgs = [m for m in brain._history if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    _assert_no_empty_names(brain._history)
    second = fake.chat.completions.calls[1]["messages"]
    assert "call_9" not in json.dumps(second)
    _assert_no_empty_names(second)


def test_nonstream_empty_name_tool_call_is_ignored(monkeypatch):
    """A (non-streamed) tool call with an empty function name is ignored instead
    of being executed and replayed, so the turn ends safely."""
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    fake = FakeClient([{"tool": ("", "{}")}])
    reply = brain.ask("do the thing", client=fake, retrieve=False)
    assert reply == "Done."
    assert [m["role"] for m in brain._history].count("tool") == 0
    _assert_no_empty_names(brain._history)


def test_reset_history():
    brain._history = [{"role": "user", "content": "a"}]
    brain.reset_history()
    assert brain._history == []
    seq = brain.ask("x", client=FakeClient([{"content": "y"}]), retrieve=False)
    assert brain._history  # non-empty again


class FlakyError(Exception):
    status_code = 429
    message = "Quota exceeded ... Please retry in 51.939530911s."


def test_rate_limit_retries_then_succeeds():
    calls = []
    script = [FlakyError(), {"content": "ok after backoff"}]

    class C:
        def create(self, **kw):
            calls.append(kw)
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return _reply_message(step.get("content", "done"))

    fake = SimpleNamespace(chat=SimpleNamespace(completions=C()))
    slept = []

    out = brain._chat_completion(fake, {"model": "m"}, sleep_fn=slept.append)
    assert out.choices[0].message.content == "ok after backoff"
    assert len(slept) == 1 and slept[0] > 0
    assert len(calls) == 2


def test_rate_limit_exhausted_raises():
    script = [FlakyError(), FlakyError()]

    class C:
        def __init__(self):
            self.i = 0

        def create(self, **kw):
            raise script[self.i]
            self.i += 1

    fake = SimpleNamespace(chat=SimpleNamespace(completions=C()))
    with pytest.raises(FlakyError):
        brain._chat_completion(fake, {"model": "m"}, sleep_fn=lambda s: None)


def test_non_rate_limit_error_propagates_immediately():
    class Other(Exception):
        status_code = 400
        message = "bad request"

    def boom(**kw):
        raise Other()

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    with pytest.raises(Other):
        brain._chat_completion(fake, {"model": "m"}, sleep_fn=lambda s: None)
    assert brain._quota_delay(Other()) == 15.0


def _poisoned_history():
    brain._history = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_orph", "type": "function",
                          "function": {"name": "", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_orph", "content": "orphan result"},
    ]


def test_ask_sanitizes_poisoned_history_before_roundtrip(monkeypatch):
    """A stale history holding an empty-name tool call plus an orphan tool
    result is the exact shape that triggers the recurring 400. ask() must heal
    it on entry so neither is ever forwarded to the model again."""
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    _poisoned_history()
    script = [
        {"tool": ("get_time", "{}")},
        {"content": "It is 10:30 PM."},
    ]
    fake = FakeClient(script)
    reply = brain.ask("tell me the time", client=fake, retrieve=False)
    assert reply == "It is 10:30 PM."
    first = fake.chat.completions.calls[0]["messages"]
    _assert_no_empty_names(first)
    assert not any(m.get("role") == "tool" and m.get("tool_call_id") == "call_orph"
                   for m in first)
    assert "call_orph" not in json.dumps(first)
    survivor = [m for m in brain._history if m["role"] == "tool"]
    assert len(survivor) == 1 and survivor[0]["tool_call_id"].startswith("call_")


def test_ask_sanitizes_before_appending_user():
    """The sanitizer runs before the user message is appended, so a poisoned
    history from a crashed turn is repaired regardless of the new prompt."""
    _poisoned_history()
    _ = brain.ask("do the thing", client=FakeClient([{"content": "Done."}]),
                  retrieve=False)
    assert brain._history[0] == {"role": "user", "content": "earlier"}
    orphan = [m for m in brain._history
              if m.get("role") == "tool" and m.get("tool_call_id") == "call_orph"]
    assert orphan == []
    _assert_no_empty_names(brain._history)


class BadRequestError(Exception):
    status_code = 400


def test_400_raises_recovers_and_retries(monkeypatch):
    """The provider rejecting a turn with a 400 (a poisoned history) must be
    self-healed: sanitize, drop the aborted turn, and retry - never crash."""
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    calls = []
    script = [BadRequestError("Name cannot be empty"), "recovered"]

    class K:
        def create(self, **kw):
            calls.append(kw)
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return _reply_message(step)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=K()))
    reply = brain.ask("do the thing", client=fake, retrieve=False)
    assert reply == "recovered"
    assert len(calls) == 2
    _assert_no_empty_names(calls[0]["messages"])
    _assert_no_empty_names(calls[1]["messages"])


def test_400_escalates_to_full_history_reset(monkeypatch):
    """If sanitizing and dropping the turn can't fix the 400, the last resort is
    a fresh history - the assistant must keep responding, not wedge forever."""
    monkeypatch.setattr(config, "RAG_ENABLED", False)

    class K:
        def __init__(self):
            self.fails = 2  # stage1 + stage2 400, stage3 (fresh history) recovers
            self.calls = []

        def create(self, **kw):
            self.calls.append(kw)
            if self.fails:
                self.fails -= 1
                raise BadRequestError("Name cannot be empty")
            return _reply_message("finally")

    fake = SimpleNamespace(chat=SimpleNamespace(completions=K()))
    reply = brain.ask("hello", client=fake, retrieve=False)
    assert reply == "finally"
    assert len(fake.chat.completions.calls) == 3
    _assert_no_empty_names(fake.chat.completions.calls[-1]["messages"])


def test_missing_tool_call_id_gets_assigned(monkeypatch):
    """A tool call arriving without an id (streamed or returned bare) must get a
    fresh call_N id so its result can be matched back when replayed."""
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    steps = [_reply_message(None, [_tool_call(None, "get_time", "{}")]),
             _reply_message("It is 9 PM.")]
    calls = []

    class NoId:
        def create(self, **kw):
            calls.append(kw)
            return steps.pop(0)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=NoId()))
    reply = brain.ask("time", client=fake, retrieve=False)
    assert reply == "It is 9 PM."
    tool_msgs = [m for m in brain._history if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    tid = tool_msgs[0]["tool_call_id"]
    assert tid.startswith("call_") and tid != ""
    second = calls[1]["messages"]
    _assert_no_empty_names(second)
    asst = [m for m in second if m["role"] == "assistant" and m.get("tool_calls")][-1]
    assert asst["tool_calls"][0]["id"] == tid
