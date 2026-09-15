"""
The "brain" of Ra.

Sends the conversation to an LLM — Gemini, Groq or NVIDIA NIM (each reached
through its OpenAI-compatible endpoint, see `config`). Relevant passages
retrieved from the local RAG index are injected when a question matches, the
model may call skills/tools, and the final reply is returned.

MULTI-BRAIN mode (two or more keys configured, see
`config.multi_brain_enabled`): every configured brain receives the same turn
in parallel and the FIRST to return a valid answer wins the race. Only the
winner ever executes tools or writes history, so tools are never run twice.
If one or two brains fail, they are logged, parked on a short cooldown, and
the survivors keep working — a broken key can never halt Ra.

Clients are created lazily (first `ask`) so everything except an actual
conversation runs without any LLM SDK installed.
"""
import json
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace

from ra import config
from ra import logging as ralog
from ra.skills import TOOLS, execute_tool

_client = None
_client_provider = None
_history = []
MAX_TOOL_ITERATIONS = config.MAX_TOOL_ITERATIONS

# Multi-brain race state. A brain that fails is parked on a short cooldown so
# it stops costing latency while the survivors answer; it re-enters afterwards.
_RACE_COOLDOWN = 10.0
_RACE_QUOTA_COOLDOWN = 150.0  # 413/429 = quota - don't re-hit a capped brain
_provider_failures = {}
_provider_clients = {}
_last_winner_provider = None
_last_winner_client = None
# Self-healing bookkeeping: `_healed_models[provider]` is the model that worked
# after the configured one was rejected (used for every later turn), and
# `_dead_models[provider]` are names already rejected this session so a dead
# model is never probed twice.
_healed_models = {}
_dead_models = {}


class HaltInterrupt(Exception):
    """Raised internally when a barge-in / 'stop' halts an in-flight reply."""


def _sleep_interruptible(delay: float, halt_event=None):
    """Sleep `delay` seconds in small slices so a barge-in can abort a 429
    backoff instead of blocking the pipeline for minutes."""
    if not delay or delay <= 0:
        return
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if halt_event is not None and halt_event.is_set():
            raise HaltInterrupt()
        time.sleep(min(0.2, max(0.001, deadline - time.monotonic())))


def _quota_delay(err) -> float:
    """Backoff seconds for a 429, parsed from the provider message if possible."""
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(getattr(err, "message", "") or ""))
    if match:
        return min(float(match.group(1)) + 1.0, 60.0)
    retry_after = getattr(err, "retry_after", None)
    if retry_after:
        return min(float(retry_after) + 1.0, 60.0)
    return 15.0


def _chat_completion(client, payload, sleep_fn=time.sleep, halt_event=None):
    """Rate-limit-aware LLM call: back off (interruptibly) and retry once on a 429."""
    for attempt in (1, 2):
        try:
            return client.chat.completions.create(**payload)
        except Exception as e:
            if attempt == 1 and getattr(e, "status_code", None) == 429:
                delay = _quota_delay(e)
                print(f"[LLM rate-limited - retrying in {delay:.0f}s...]")
                if halt_event is not None:
                    _sleep_interruptible(delay, halt_event)
                else:
                    sleep_fn(delay)
                continue
            raise


def _tag_provider(client, provider) -> None:
    """Remember which brain/provider a client belongs to so the race can pick
    the right model per brain without extra plumbing."""
    try:
        client.__ra_provider = provider
    except Exception:
        pass


def get_client():
    global _client, _client_provider
    if _client is None:
        provider = config.get_provider()
        if provider in ("gemini", "nim"):
            # Both are reached through the OpenAI SDK; only the key/endpoint
            # differ. (nim used to fall into the groq branch below, which sent
            # the Groq key to the Groq endpoint - a silently unusable client.)
            try:
                from openai import OpenAI
            except ImportError:
                raise RuntimeError(
                    "The 'openai' package is not installed - install it with "
                    "`pip install -r requirements.txt` to talk to the LLM."
                )
            if provider == "nim":
                _client = OpenAI(
                    api_key=config.get_nim_api_key(),
                    base_url=config.NIM_BASE_URL,
                )
            else:
                _client = OpenAI(
                    api_key=config.get_gemini_api_key(),
                    base_url=config.GEMINI_BASE_URL,
                )
        else:
            try:
                from groq import Groq
            except ImportError:
                raise RuntimeError(
                    "The 'groq' package is not installed - install it with "
                    "`pip install -r requirements.txt` to talk to the LLM."
                )
            _client = Groq(api_key=config.get_api_key())
        _tag_provider(_client, provider)
        _client_provider = provider
    return _client


def _fallback_client():
    """Build a client on ANOTHER provider so a rate-limited/failing provider
    never leaves the user stuck (the 'neural switchboard' pattern - automatic
    provider failover). Returns (client, provider_name) or raises when no
    fallback is configured."""
    from ra import config as _cfg
    primary = _cfg.get_provider()
    candidates = [p for p in _cfg.LLM_PROVIDERS if p != primary]
    # Prefer a brain that is NOT parked on a cooldown: a model we just saw
    # rejected (404) or a key that was refused (401) must not be handed the
    # failover turn. Parked brains stay on the list as a last resort so the
    # user is never left with nothing at all. (list.sort is stable.)
    candidates.sort(key=lambda p: (not _provider_ok(p),))
    for provider in candidates:
        if provider == "gemini" and not _cfg.gemini_available():
            continue
        if provider == "nim" and not _cfg.nim_available():
            continue
        if provider == "groq" and not _cfg.groq_enabled():
            continue
        if provider == "gemini":
            from openai import OpenAI
            cli = OpenAI(
                api_key=_cfg.get_gemini_api_key(),
                base_url=_cfg.GEMINI_BASE_URL,
            )
        elif provider == "nim":
            from openai import OpenAI
            cli = OpenAI(
                api_key=_cfg.get_nim_api_key(),
                base_url=_cfg.NIM_BASE_URL,
            )
        else:
            from groq import Groq
            cli = Groq(api_key=_cfg.get_api_key())
        _tag_provider(cli, provider)
        return cli, provider
    raise RuntimeError("No fallback LLM provider configured.")


def _make_client(provider: str):
    """Build (and cache) an OpenAI-compatible client for a named brain.
    Cached per provider; `force=True` recreates it."""
    if provider in _provider_clients:
        return _provider_clients[provider]
    from openai import OpenAI
    if provider == "gemini":
        cli = OpenAI(
            api_key=config.get_gemini_api_key(),
            base_url=config.GEMINI_BASE_URL,
        )
    elif provider == "groq":
        cli = OpenAI(api_key=config.get_api_key(),
                     base_url=config.GROQ_BASE_URL)
    elif provider == "nim":
        cli = OpenAI(
            api_key=config.get_nim_api_key(),
            base_url=config.NIM_BASE_URL,
        )
    else:
        raise ValueError(f"Unknown provider '{provider}'")
    _tag_provider(cli, provider)
    _provider_clients[provider] = cli
    return cli


def _provider_ok(name: str) -> bool:
    """True when a brain is not parked on its failure cooldown."""
    until = _provider_failures.get(name)
    if until is None:
        return True
    if time.monotonic() >= until:
        del _provider_failures[name]
        return True
    return False


def _is_quota_error(exc) -> bool:
    """True when a provider failure is a quota/rate-limit class error (413
    'request too large', 429 rate limited, quota exceeded). Those deserve a
    LONG cooldown - re-trying a capped brain every turn is pure noise."""
    msg = str(exc).lower()
    return ("413" in msg or "429" in msg or "rate_limit" in msg
            or "quota" in msg or "request too large" in msg)


def _is_dead_config_error(exc) -> bool:
    """True when a brain is unusable for a CONFIGURATION reason rather than load:
    a model that no longer exists for this account (404 'not found for account'
    - providers decommission hosted models without notice), a rejected or
    missing key (401/403), or an unrecognized model name.

    These never fix themselves on a retry. Without this, a dead brain was
    retried on every single turn, dumping a full traceback each time and
    wasting a race slot - the 'errors appearing all the time' symptom."""
    code = getattr(exc, "status_code", None)
    if code in (401, 403, 404):
        return True
    msg = str(getattr(exc, "message", "") or exc).lower()
    return any(k in msg for k in (
        "not found for account", "notfounderror", "does not exist",
        "no such model", "unrecognized model", "invalid api key",
        "invalid_api_key", "unauthorized", "model_not_found",
    ))


def _is_model_missing_error(exc) -> bool:
    """True when the MODEL is what the provider rejected (renamed, decommissioned,
    or simply not hosted for this account) as opposed to a key/policy problem
    that would fail every model alike. Only these deserve a self-heal retry on a
    sibling model; an auth failure would just burn those calls too."""
    msg = str(getattr(exc, "message", "") or exc).lower()
    if any(k in msg for k in (
        "not found for account", "notfounderror", "does not exist",
        "no such model", "unrecognized model", "model_not_found",
        "is no longer available", "not supported for",
    )):
        return True
    return (getattr(exc, "status_code", None) == 404
            and "unauthorized" not in msg and "invalid api key" not in msg)


def _debug_enabled() -> bool:
    """True when RA_DEBUG is set: stack traces are printed for brain failures.
    Off by default - a repeatedly failing brain must not flood the log."""
    return os.environ.get("RA_DEBUG", "").strip().lower() in ("1", "on", "yes", "true")


def _mark_down(name: str, cooldown: float | None = None, reason: str = "") -> None:
    """Park a failing brain on a cooldown and drop its cached client so the
    next attempt builds a fresh one. The race never lets one failure take the
    others with it. Quota-class AND dead-config failures get the long cooldown
    (neither heals on a retry, so re-hitting them every turn is pure noise)."""
    if cooldown is None:
        cooldown = _RACE_COOLDOWN
    where = f" - {reason}" if reason else ""
    print(f"[brain {name} parked {cooldown:.0f}s{where}; others carry on]")
    _provider_failures[name] = time.monotonic() + cooldown
    _provider_clients.pop(name, None)


def _is_retryable(exc) -> bool:
    """True when the provider is rate-limited or temporarily unavailable and a
    different provider (or a retry) is worth attempting."""
    code = getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    msg = str(getattr(exc, "message", "") or exc).lower()
    return any(k in msg for k in ("rate limit", "quota", "overloaded",
                                  "temporarily unavailable", "503", "502",
                                  "connection error", "timed out", "timeout"))


def _to_openai_tools():
    """Convert our tool schemas into the OpenAI-style format Groq and Gemini both accept."""
    converted = []
    for t in TOOLS:
        converted.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        })
    return converted


_TOOLS = _to_openai_tools()


def _exec_one(tc):
    """Run a single tool call (parallelisable). Returns (args, result_text)."""
    name = tc.function.name
    try:
        args = json.loads(tc.function.arguments)
    except Exception:
        args = {}
    from ra import redact
    ralog.log("tool", f"call: {name}({redact.log_safe(json.dumps(args, default=str))})")
    result = execute_tool(name, args)
    ralog.log("ok", f"{name} -> {redact.log_safe(str(result))}")
    return args, str(result)


def _retrieve_context(question: str, top_k=None, min_score=None) -> str | None:
    if not (config.RAG_ENABLED and config.RAG_AUTO_RETRIEVE):
        return None
    try:
        from ra.rag.embedder import HashEmbedder
        from ra.rag.retriever import retrieve, format_context
        from ra.rag.store import SemanticStore
        store = SemanticStore(config.RAG_INDEX_PATH)
        embedder = HashEmbedder(dim=config.RAG_EMBED_DIM)
        results = retrieve(store, embedder, question, k=top_k or config.RAG_TOP_K)
        threshold = min_score if min_score is not None else config.RAG_MIN_SCORE
        if not results or results[0].score < threshold:
            return None
        return format_context(results)
    except Exception as e:
        print(f"[rag context error: {e}]")
        return None


def _stream_turn(client, payload, on_token=None, halt_event=None):
    """Stream one LLM turn; accumulates text deltas and tool-call fragments.
    Returns (content, tool_calls_or_None) where each tool call is a
    SimpleNamespace(id, function.name, function.arguments, extra_content)."""
    chunks = _chat_completion(client, dict(payload, stream=True), halt_event=halt_event)
    content = []
    tool_index = {}
    for chunk in chunks:
        if halt_event is not None and halt_event.is_set():
            break
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        piece = getattr(delta, "content", None)
        if piece:
            content.append(piece)
            if on_token:
                on_token(piece)
        for tc in (getattr(delta, "tool_calls", None) or []):
            idx = getattr(tc, "index", 0)
            slot = tool_index.setdefault(
                idx, {"id": None, "name": "", "args": "", "extra": None}
            )
            tc_id = getattr(tc, "id", None)
            if tc_id:
                slot["id"] = tc_id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] += fn.name
                if getattr(fn, "arguments", None):
                    slot["args"] += fn.arguments
            extra = getattr(tc, "extra_content", None)
            if extra:
                slot["extra"] = extra
    tool_calls = None
    built = []
    for idx in sorted(tool_index):
        slot = tool_index[idx]
        if not slot["name"].strip():
            continue
        built.append(SimpleNamespace(
            id=slot["id"] or f"call_{idx}",
            function=SimpleNamespace(name=slot["name"].strip(), arguments=slot["args"]),
            extra_content=slot["extra"],
        ))
    if built:
        tool_calls = built
    return "".join(content), tool_calls


_call_seq = 0


def _next_call_id() -> str:
    global _call_seq
    _call_seq += 1
    return f"call_{_call_seq}"


def _clean_tool_calls(tool_calls):
    """Keep only tool calls whose function name is non-empty, and give every
    kept call a usable id. Returns None when nothing valid remains."""
    if not tool_calls:
        return None
    kept = []
    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        if not str(getattr(fn, "name", "") or "").strip():
            continue
        if not str(getattr(tc, "id", "") or "").strip():
            tc.id = _next_call_id()
        kept.append(tc)
    return kept or None


def _sanitize_history():
    """Validate _history in place: drop empty-name tool calls and any tool
    result whose tool_call_id has no matching, named assistant tool call ahead
    of it. This is what keeps a malformed turn from ever replaying a
    `function_response` with an empty name into Gemini (the recurring 400)."""
    global _history
    clean = []
    active_ids = None
    for m in _history:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            kept = []
            for tc in m["tool_calls"]:
                name = str((tc.get("function") or {}).get("name") or "").strip()
                if not name:
                    continue
                tc_id = str(tc.get("id") or "").strip()
                if not tc_id:
                    tc_id = _next_call_id()
                    tc["id"] = tc_id
                kept.append(tc)
            if kept:
                m = dict(m, tool_calls=kept)
                active_ids = {tc["id"] for tc in kept}
            else:
                m = {k: v for k, v in m.items() if k != "tool_calls"}
                active_ids = None
        elif role == "tool":
            if not active_ids or str(m.get("tool_call_id") or "") not in active_ids:
                continue
        else:
            active_ids = None
        clean.append(m)
    _history = clean


def _model_for(client) -> str:
    """Model name for `client`'s brain. Uses the tagged provider so each brain
    in the race gets the right model; falls back to the active provider. A brain
    that self-healed keeps using the model that actually answered."""
    provider = getattr(client, "__ra_provider", None)
    if provider and provider in _healed_models:
        return _healed_models[provider]
    return config.model_for(provider) if provider else config.get_llm_model()


def _safe_model(provider: str) -> str:
    """Model name for a provider, never raising (used in error messages so a
    misconfigured brain reports WHICH model was rejected)."""
    if provider in _healed_models:
        return _healed_models[provider]
    try:
        return config.model_for(provider)
    except Exception:
        return "?"


def _model_candidates_for(client) -> list:
    """Models to try for `client`, the one it should be using FIRST. Prepends the
    brain's healed model so a self-healed brain never falls back to a dead one."""
    provider = getattr(client, "__ra_provider", None)
    now = _model_for(client)
    if not provider:
        return [now]
    out = [now]
    for name in config.model_candidates(provider):
        if name not in out:
            out.append(name)
    return out


def _converse_turn(client, base_messages, stream, on_token=None, halt_event=None):
    """One LLM model call for `client`. Returns (content, tool_calls) where
    tool_calls are cleaned (or None). Streams content via `on_token` when
    stream=True (single-brain path).

    Self-heals a rejected model exactly like the race does: a 404 for the model
    itself retries the same turn on a sibling model, so single-brain mode can
    never be wedged for a whole session by one retired model name."""
    provider = getattr(client, "__ra_provider", None)
    last_err = None
    for model in _model_candidates_for(client):
        if model in _dead_models.get(provider or "", ()):
            continue
        payload = {
            "model": model,
            "messages": base_messages + _history,
            "tools": _TOOLS,
            "max_tokens": config.MAX_TOKENS,
        }
        try:
            if stream:
                content, tool_calls = _stream_turn(client, payload, on_token, halt_event)
                content = content or ""
                tool_calls = _clean_tool_calls(tool_calls)
            else:
                response = _chat_completion(client, payload, halt_event=halt_event)
                msg = response.choices[0].message
                content = msg.content or ""
                tool_calls = _clean_tool_calls(msg.tool_calls)
        except Exception as e:
            if provider and _is_model_missing_error(e):
                last_err = e
                _dead_models.setdefault(provider, set()).add(model)
                ralog.log("warn", f"brain {provider}: model '{model}' unusable - "
                                  "trying a sibling")
                continue
            raise
        if provider:
            _healed_models[provider] = model
        return content, tool_calls
    if last_err is not None:
        raise last_err
    raise RuntimeError("No usable model configured for this brain.")


def _tool_loop(client, base_messages, stream, on_token, on_tool, halt_event,
               initial_content="", initial_tool_calls=None) -> str:
    """Run the assistant/model/tool loop for ONE user turn, appending assistant
    and tool messages to _history as it goes. `initial_*` injects the
    race-winning turn so the winner's answer is never asked for twice. Only
    this loop ever mutates _history (losers were read-only). Raises provider
    errors to the caller so ask() can recover the history and retry."""
    reply = ""
    have_initial = bool(initial_content) or initial_tool_calls is not None
    msg_content = initial_content
    tool_calls = initial_tool_calls
    iterations = 0
    while True:
        iterations += 1
        if iterations > MAX_TOOL_ITERATIONS:
            reply = "Sorry, I got stuck trying to use my tools too many times."
            break
        if halt_event is not None and halt_event.is_set():
            break
        if not have_initial or iterations > 1:
            msg_content, tool_calls = _converse_turn(
                client, base_messages, stream, on_token, halt_event)

        if halt_event is not None and halt_event.is_set():
            # Aborted mid-reply: record what was said so far, and never
            # queue tool calls we no longer intend to execute.
            reply = msg_content.strip()
            if msg_content:
                _history.append({"role": "assistant", "content": msg_content})
            break

        assistant_entry = {"role": "assistant", "content": msg_content}
        if tool_calls:
            replayed = []
            for tc in tool_calls:
                entry = {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                extra = getattr(tc, "extra_content", None)
                if extra:
                    # Gemini 3.x requires the model-issued thought_signature to be
                    # replayed verbatim when continuing a tool-calling turn.
                    entry["extra_content"] = extra
                replayed.append(entry)
            assistant_entry["tool_calls"] = replayed
        _history.append(assistant_entry)

        if not tool_calls:
            reply = msg_content.strip()
            break

        # Run every tool the model requested IN PARALLEL (tools are
        # independent) so multi-step replies finish in one round-trip.
        results = [None] * len(tool_calls)
        with ThreadPoolExecutor(max_workers=min(4, len(tool_calls))) as ex:
            futures = {ex.submit(_exec_one, tc): i
                       for i, tc in enumerate(tool_calls)}
            for fut in as_completed(futures):
                idx = futures[fut]
                results[idx] = fut.result()
                if halt_event is not None and halt_event.is_set():
                    for other in futures:
                        other.cancel()
                    break
        for i, tc in enumerate(tool_calls):
            if results[i] is None:
                continue
            args, result = results[i]
            if on_tool:
                on_tool(tc.function.name, args, result)
            _history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
    return reply


def _converse(client, base_messages, stream, on_token, on_tool, halt_event,
              initial_content="", initial_tool_calls=None) -> str:
    """Run the model/tool loop for one user turn (single-brain path)."""
    try:
        return _tool_loop(client, base_messages, stream, on_token, on_tool,
                          halt_event, initial_content, initial_tool_calls)
    except HaltInterrupt:
        return "Stopped."


def _call_once(client, model, base_messages, halt_event):
    """One read-only model call on `model`. Returns (client, content, tool_calls)."""
    payload = {
        "model": model,
        "messages": base_messages + _history,
        "tools": _TOOLS,
        "max_tokens": config.MAX_TOKENS,
    }
    response = _chat_completion(client, payload, halt_event=halt_event)
    msg = response.choices[0].message
    return client, msg.content or "", _clean_tool_calls(msg.tool_calls)


def _race_call_one(client, base_messages, halt_event):
    """One brain's entry in the race: a read-only single model call. Returns
    (client, content, tool_calls) on success; the caller catches failures so a
    dead brain is simply skipped, never an error.

    SELF-HEALING: if the provider rejects the MODEL itself (404 'not found for
    account' - hosted models are retired without notice), the brain is not lost
    for the session. The same turn is retried on the next candidate model and
    whichever answers becomes the brain's model from then on."""
    provider = getattr(client, "__ra_provider", None)
    model = _model_for(client)
    try:
        return _call_once(client, model, base_messages, halt_event)
    except Exception as e:
        if not (provider and _is_model_missing_error(e)):
            raise
        _dead_models.setdefault(provider, set()).add(model)
        for alt in _model_candidates_for(client)[1:]:
            if alt in _dead_models[provider]:
                continue
            try:
                result = _call_once(client, alt, base_messages, halt_event)
            except Exception:
                _dead_models[provider].add(alt)
                continue
            _healed_models[provider] = alt
            ralog.log("ok", f"brain {provider}: model '{model}' unusable - "
                            f"now using '{alt}'")
            print(f"[brain {provider}: '{model}' -> '{alt}']")
            return result
        raise


def _race_first_turn(base_messages, halt_event):
    """Multi-brain race: every configured brain gets the SAME turn
    simultaneously; the FIRST brain with a valid answer wins and everyone else
    is thrown away. Failed brains are logged + parked on cooldown and the
    survivors keep running, so one-or-two-down never becomes an error.
    Returns (client, provider, content, tool_calls) for the winner, or
    (None, None, None, None) when every brain failed."""
    providers = [p for p in config.configured_brains() if _provider_ok(p)]
    if not providers:
        return None, None, None, None

    # The winner's takeaway is never gated on the slowest brain: the executor
    # is shut down WITHOUT waiting, and any slower losers are simply cancelled.
    ex = ThreadPoolExecutor(max_workers=len(providers))
    futures = {}
    try:
        for provider in providers:
            try:
                cli = _make_client(provider)
            except Exception as e:
                _mark_down(provider)
                ralog.log("warn", f"multi-brain: {provider} not configured ({e})")
                continue
            futures[ex.submit(_race_call_one, cli, base_messages, halt_event)] = provider

        for fut in as_completed(futures):
            provider = futures[fut]
            try:
                cli, content, tool_calls = fut.result()
            except HaltInterrupt:
                continue
            except Exception as e:
                ralog.log("warn", f"multi-brain: {provider} failed ({e})")
                # A rejected model/key (404/401) or a capped account (413/429)
                # does NOT heal on a retry. Park the brain for the LONG window
                # so the survivors answer at full speed, the per-turn retry
                # disappears, and the log stops filling with tracebacks.
                dead = _is_dead_config_error(e)
                capped = _is_quota_error(e)
                if dead:
                    reason = f"model '{_safe_model(provider)}' rejected/unavailable"
                elif capped:
                    reason = "rate limit/quota"
                else:
                    reason = type(e).__name__
                if _debug_enabled():
                    traceback.print_exc()
                _mark_down(provider,
                           _RACE_QUOTA_COOLDOWN if (dead or capped) else None,
                           reason)
                continue
            if content.strip() or tool_calls:
                # Winner. Anyone still running is irrelevant - drop them.
                for other in futures:
                    if other is not fut:
                        other.cancel()
                return cli, provider, content, tool_calls
            # Empty-but-valid responses also count as a failure (a reply that
            # is nothing is not a usable answer for the user).
            _mark_down(provider)
        return None, None, None, None
    finally:
        # wait=False: never slow the winner down for the slowest brain.
        ex.shutdown(wait=False)


def _ask_multi(base_messages, stream, on_token, on_tool, halt_event) -> str:
    """Multi-brain turn: race every configured brain, then let the WINNER run
    the tool loop (tools execute exactly once - on the winner only). Raises a
    clear error only when every single brain failed."""
    global _last_winner_provider, _last_winner_client
    client, provider, content, tool_calls = _race_first_turn(base_messages, halt_event)
    if client is None:
        parked = ", ".join(sorted(_provider_failures)) or "none"
        raise RuntimeError(
            "All brains failed on this one - none of them could answer. "
            f"(parked on cooldown: {parked}) Check each brain's model/API key; "
            "the [brain ... parked] notes above name the model that was rejected."
        )
    _last_winner_provider = provider
    _last_winner_client = client
    # The winner's first-turn text was produced off-stream (so the race could
    # compare answers); replay it to the HUD when streaming so the user still
    # sees the reply flow in live.
    if stream and on_token is not None and content:
        on_token(content)
    return _converse(client, base_messages, stream, on_token, on_tool,
                     halt_event, initial_content=content, initial_tool_calls=tool_calls)


def _is_400(exc) -> bool:
    return getattr(exc, "status_code", None) == 400 or (
        "BadRequest" in type(exc).__name__
    )


def _recover_from_400(exc, user_text, client, base_messages, user_idx,
                      stream, on_token, on_tool, halt_event) -> str:
    """A 400 usually means our history holds something the provider's bridge
    can't replay. Repair progressively - sanitize, drop the aborted turn, then
    forget the conversation - so one bad turn never wedges the assistant."""
    global _history
    for stage in (1, 2, 3):
        if stage == 2:
            del _history[user_idx + 1:]
        elif stage == 3:
            _history = [{"role": "user", "content": user_text}]
        _sanitize_history()
        try:
            return _converse(client, base_messages, stream,
                             on_token, on_tool, halt_event)
        except Exception as again:
            if not _is_400(again):
                raise
            continue
    raise exc


def ask(user_text: str, *, client=None, retrieve: bool = True,
        stream: bool = False, on_token=None, on_tool=None,
        halt_event=None) -> str:
    """Ask Ra a question. `client` may be injected (tests/fakes); stream=True
    streams reply text through `on_token` (used by the HUD for live answers);
    `on_tool(name, args, result)` fires for every tool call. `retrieve=False`
    skips the automatic RAG context injection. `halt_event` aborts the whole
    reply (LLM streaming + pending tool calls) the moment it is set - used by
    barge-in / 'Ra, stop' so a long task can be killed immediately."""
    global _history
    if halt_event is not None and halt_event.is_set():
        return ""
    _sanitize_history()
    _history.append({"role": "user", "content": user_text})

    # Fast lane: deterministic, offline answer for well-formed trivial queries
    # (time/date, battery, system status, timers). Skips the LLM round-trip.
    from ra import fastlane
    fast = fastlane.try_answer(user_text)
    if fast:
        if stream and on_token is not None:
            on_token(fast)
        _history.append({"role": "assistant", "content": fast})
        return fast

    if not config.multi_brain_enabled():
        client = client or get_client()

    context = _retrieve_context(user_text) if retrieve else None
    memory_ctx = None
    if retrieve:
        try:
            from ra import memory
            memory_ctx = memory.inject_context(user_text)
        except Exception as e:
            ralog.log("warn", f"memory inject failed: {e}")
    base_messages = [{"role": "system", "content": config.SYSTEM_PROMPT}]
    if memory_ctx:
        base_messages.append({"role": "system", "content": memory_ctx})
    if context:
        base_messages.append({
            "role": "system",
            "content": (
                "Relevant passages retrieved from the user's own local data "
                "(indexed files, notes, device snapshots, screen captures). "
                "Prefer these when they answer the question, and name the source.\n\n"
                + context
            ),
        })

    reply = ""
    try:
        if config.multi_brain_enabled():
            reply = _ask_multi(base_messages, stream, on_token, on_tool, halt_event)
        else:
            reply = _converse(client, base_messages, stream,
                              on_token, on_tool, halt_event)
    except Exception as exc:
        if _is_retryable(exc) and not stream:
            # Provider failover: rate-limited or down -> try a fresh client on
            # the other provider once, so a dead key never wedges the session.
            try:
                global _client
                global _last_winner_provider
                # Discard any partial assistant/tool turns the failed call left.
                # Keep the user message so the re-try has full context.
                user_idx = len(_history) - 1
                if user_idx >= 0:
                    del _history[user_idx + 1:]
                if _last_winner_provider:
                    _mark_down(_last_winner_provider)
                    _last_winner_provider = None
                _client = None
                alt = _fallback_client()
                ralog.log("warn", f"failing over to {alt[1]} after: {exc}")
                reply = _converse(alt[0], base_messages, stream,
                                  on_token, on_tool, halt_event)
            except Exception as exc2:
                _client = None
                raise exc2
        elif not _is_400(exc):
            raise
        else:
            # Multi-brain route: the winning brain's client is the one that owns
            # THIS turn - replay the healing loop on it, not the (unused) default.
            # (A stale winner from an earlier turn is never reused for a plain
            # single-brain call, which owns its own `client`.)
            recover_client = (_last_winner_client
                              if config.multi_brain_enabled() else client)
            reply = _recover_from_400(exc, user_text, recover_client, base_messages,
                                      len(_history) - 1, stream,
                                      on_token, on_tool, halt_event)
    if not reply:
        reply = "Done."

    _sanitize_history()
    _trim_history()
    return reply


def _trim_history():
    global _history
    max_items = config.MAX_HISTORY_TURNS * 4
    if len(_history) > max_items:
        _history = _history[-max_items:]
        _sanitize_history()


def reset_history():
    global _history
    _history = []