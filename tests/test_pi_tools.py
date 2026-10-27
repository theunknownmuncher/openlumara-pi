# -- AI GENERATED CODE (OpenLumara agent, model Qwen3.8-Flash-Next) :: (2026-10-26) status:unreviewed
#
# pi module tests for OpenLumara
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
the pi module's tools as OpenLumara actually calls them.

integration level: the REAL registration surface (core.tool_loader.ToolLoader
cataloging the real module instance) and the REAL dispatch loop
(core.toolcalls.ToolcallManager.process driving assistant tool_calls, the same
code path every channel uses) on FakeChannel/FakeAPI + real Context.

only pi itself is faked for the fast tests: a scripted FakeRPC answers the
session's command calls with the exact event records pi's RPC mode emits
(agent_start / message_end / agent_settled), so the module's own run folding,
settle waiters and result summaries are exercised for real without needing an
authenticated pi provider. a live test (marked integration, opt-in via PI_LIVE)
drives a real pi through the same pipeline.

intent under test:
- the module registers exactly its documented pi_* tools, with correct
  names, required args and typed schemas (and does not clobber the base
  Module.result helper).
- tools_load(["pi"]) (dynamic tool loading, the default mode) delivers all
  pi_* schemas to the model.
- pi_run through process(): prompt is sent, pi's event stream is folded into
  a run record, and the role=tool message the model receives carries the
  answer + tool usage as serializable JSON.
- error paths (invalid session name, missing pi binary) come back as
  status=error tool results instead of killing the agentic chain.
- pi_status / pi_get_result / pi_sessions / pi_abort / pi_send / pi_set_model
  all work through the pipeline, including provider/id model splitting.
"""

import asyncio
import json
import os

import pytest

import core
from harness import apply_config

# ---------------------------------------------------------------------------
# module loading (the way core.modules does it at runtime)
# ---------------------------------------------------------------------------

_ns = core.modules.import_entry(__import__("user_modules"), "pi")
Pi = _ns.Pi
PiSession = _ns.PiSession
PiRPCError = _ns.PiRPCError

EXPECTED_TOOLS = {
    "pi_run", "pi_send", "pi_get_result", "pi_steer", "pi_abort",
    "pi_status", "pi_sessions", "pi_close", "pi_models", "pi_set_model",
}

MODEL_CONFIG = dict(
    api={"max_context": 16768},
    model={
        "name": "test-model",
        "use_tools": True,
        "dynamic_tool_loading": True,
        "keep_reasoning_in_context": False,
        "only_preserve_reasoning_for_current_agentic_loop": False,
        "automatically_compress_context": False,
        "context_compression_threshold": 0.8,
    },
    modules={"disabled_prompts": []},
    core={"tool_timeout": 60},
)


# ---------------------------------------------------------------------------
# scripted pi RPC (records mirror docs/json.md in the pi package)
# ---------------------------------------------------------------------------

class FakeRPC:
    """stands in for PiRPCClient: same surface, canned but protocol-shaped answers"""

    def __init__(self, answer="42 files reviewed, no issues"):
        self.answer = answer
        self.alive = True
        self.calls = []           # (command, kwargs) tuples for assertions
        self._events = asyncio.Queue()

    async def next_event(self, timeout=None):
        return await self._events.get()

    async def prompt(self, message, streaming_behavior=None, timeout=60.0):
        self.calls.append(("prompt", {"message": message, "streaming": streaming_behavior}))
        self._events.put_nowait({"type": "agent_start"})
        self._events.put_nowait({
            "type": "message_end",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": self.answer}],
                        "stopReason": "stop"},
        })
        self._events.put_nowait({"type": "tool_execution_start", "toolCallId": "c1",
                                 "toolName": "read", "args": {}})
        self._events.put_nowait({"type": "agent_settled", "aborted": False})
        return {"disposition": "started"}

    async def follow_up(self, message):
        self.calls.append(("follow_up", {"message": message}))
        return {"disposition": "queued"}

    async def steer(self, message):
        self.calls.append(("steer", {"message": message}))
        return {"disposition": "queued"}

    async def abort(self):
        self.calls.append(("abort", {}))
        return None

    async def clear_queue(self):
        self.calls.append(("clear_queue", {}))
        return {"steering": ["mid-task redirect"], "followUp": []}

    async def get_state(self):
        return {"model": {"provider": "openlumara", "id": "test-model"},
                "thinkingLevel": "medium", "isStreaming": False, "messageCount": 5,
                "sessionFile": "/tmp/sessions/fake.jsonl", "sessionName": "default"}

    async def get_session_stats(self):
        return {"tokens": {"total": 1234}, "cost": 0.01,
                "contextUsage": {"tokens": 900, "contextWindow": 16768, "percent": 5}}

    async def get_available_models(self):
        return {"models": [{"provider": "openlumara", "id": "test-model"},
                           {"provider": "other", "id": "big-model"}]}

    async def set_model(self, model_id, provider=None):
        self.calls.append(("set_model", {"model_id": model_id, "provider": provider}))
        return None

    async def set_thinking_level(self, level):
        self.calls.append(("set_thinking_level", {"level": level}))
        return None

    async def get_last_assistant_text(self):
        return {"text": self.answer}

    async def stop(self):
        self.alive = False

    def stderr_summary(self):
        return ""


# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------

class QuietLoaderChannel:
    """ToolLoader only logs through the channel"""

    def __init__(self, manager):
        self.manager = manager

    def log(self, category, message):
        pass


def make_pi(fake_manager, **settings):
    """real module instance on the isolated test config, settings baked in"""
    apply_config(user_modules={"settings": {"pi": settings}}, **MODEL_CONFIG)
    return Pi(fake_manager, is_user_module=True)


async def make_env(fake_channel, pi_mod, tool_names=None):
    """real Context + real ToolLoader + real ToolcallManager, like every channel builds.

    NOTE: must NOT re-apply the config here - make_pi already loaded it, and a
    fresh apply_config would wipe the user_modules.settings.pi section the
    module reads at session start (extra_args and friends).
    """
    ctx = core.context.Context(fake_channel)
    fake_channel.context = ctx
    await ctx.chat.autoload()

    loader = core.tool_loader.ToolLoader(fake_channel)
    loader.register_module(pi_mod)
    fake_channel.tool_loader = loader
    fake_channel.manager.modules = {"pi": pi_mod}

    names = tool_names if tool_names is not None else list(loader.catalog)
    fake_channel.manager.tools = [{"type": "function", "function": {"name": n}} for n in names]

    tc = core.toolcalls.ToolcallManager(fake_channel)
    fake_channel.tc_manager = tc
    return ctx, loader, tc


async def attach_fake_session(pi_mod, name="default", answer="all good"):
    """a real PiSession (pump, waiters, run folding) riding a FakeRPC.

    _get_session is replaced with one that hands out this session but still
    reproduces the real binary check, so 'pi vanished' paths stay honest.
    """
    rpc = FakeRPC(answer=answer)
    session = PiSession(pi_mod, name, "/tmp/pi-test")
    session.rpc = rpc
    session.pump = asyncio.create_task(session._pump())
    pi_mod.live_sessions[name] = session

    async def fake_get_session(n, cwd=""):
        if pi_mod.binary is None:
            raise PiRPCError("pi binary not found. Install it with "
                             "'npm install -g @earendil-works/pi-coding-agent' "
                             "or set the pi_path setting.")
        return session

    pi_mod._get_session = fake_get_session
    return session, rpc


def tool_call(id_, name, arguments):
    return {"id": id_, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def assistant_with_tools(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


async def collect(tc, msg):
    return [token async for token in tc.process(msg)]


def tool_payloads(tokens):
    return [json.loads(t["content"]) for t in tokens if t.get("type") == "tool"]


# ---------------------------------------------------------------------------
# registration & schemas
# ---------------------------------------------------------------------------

async def test_registers_documented_tools_with_schemas(fake_manager):
    pi_mod = make_pi(fake_manager)
    assert pi_mod.name == "pi"
    assert pi_mod.category == "development"

    loader = core.tool_loader.ToolLoader(QuietLoaderChannel(fake_manager))
    loader.register_module(pi_mod)
    assert set(loader.catalog) == EXPECTED_TOOLS

    run = loader.catalog["pi_run"]["tool"]["function"]
    assert run["parameters"]["required"] == ["task"]
    assert run["parameters"]["properties"]["background"]["type"] == "boolean"
    assert run["parameters"]["properties"]["session"]["type"] == "string"
    assert "pi coding agent" in run["description"]

    # the get_result tool must not have shadowed the base result() helper
    assert callable(pi_mod.result) and not getattr(pi_mod.result, "_is_tool", False)
    assert loader.catalog["pi_get_result"]["method"] == "get_result"


async def test_tools_load_meta_delivers_pi_schemas(fake_manager):
    pi_mod = make_pi(fake_manager)
    loader = core.tool_loader.ToolLoader(QuietLoaderChannel(fake_manager))
    loader.register_module(pi_mod)
    fake_manager.modules = {"pi": pi_mod}

    result = await loader.tools_load(["pi"])
    assert result["status"] == "success"
    assert {t["name"] for t in result["tools"]} == EXPECTED_TOOLS


# ---------------------------------------------------------------------------
# dispatch through the real agentic loop
# ---------------------------------------------------------------------------

async def test_pi_run_round_trip_through_process(fake_manager, fake_channel):
    pi_mod = make_pi(fake_manager)
    ctx, _loader, tc = await make_env(fake_channel, pi_mod)
    session, rpc = await attach_fake_session(pi_mod, answer="fixed 3 tests")

    tokens = await collect(tc, assistant_with_tools(
        tool_call("c1", "pi_run", {"task": "fix the failing tests"})))

    payload = tool_payloads(tokens)[0]
    assert payload["status"] == "success"
    assert payload["content"]["status"] == "completed"
    assert payload["content"]["response"] == "fixed 3 tests"
    assert payload["content"]["tools_used"] == {"read": 1}

    # the prompt really reached pi, and the tool message reached the chat store
    assert rpc.calls[0][0] == "prompt"
    assert rpc.calls[0][1]["message"] == "fix the failing tests"
    history = await ctx.chat.messages.get()
    tool_msgs = [m for m in history if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "c1"
    assert "fixed 3 tests" in tool_msgs[0]["content"]

    await session.stop()


async def test_error_paths_survive_the_chain(fake_manager, fake_channel):
    pi_mod = make_pi(fake_manager)
    _ctx, _loader, tc = await make_env(fake_channel, pi_mod)
    session, _rpc = await attach_fake_session(pi_mod)

    pi_mod.binary = None  # pretend the pi binary vanished mid-session
    tokens = await collect(tc, assistant_with_tools(
        tool_call("c1", "pi_run", {"task": "x", "session": "../evil"}),   # invalid name
        tool_call("c2", "pi_models", {}),                                  # binary gone
    ))

    payloads = tool_payloads(tokens)
    assert [p["status"] for p in payloads] == ["error", "error"]
    assert "invalid session name" in str(payloads[0]["content"])
    assert "pi binary not found" in str(payloads[1]["content"])
    # the chain continued to the second call instead of aborting

    await session.stop()


async def test_status_result_sessions_through_process(fake_manager, fake_channel):
    pi_mod = make_pi(fake_manager)
    _ctx, _loader, tc = await make_env(fake_channel, pi_mod)
    session, _rpc = await attach_fake_session(pi_mod, answer="done!")

    await collect(tc, assistant_with_tools(tool_call("c1", "pi_run", {"task": "do it"})))

    tokens = await collect(tc, assistant_with_tools(
        tool_call("c2", "pi_status", {}),
        tool_call("c3", "pi_get_result", {"include_text": True}),
        tool_call("c4", "pi_sessions", {}),
    ))
    status, result, sessions = tool_payloads(tokens)

    assert status["content"]["model"] == "openlumara/test-model"
    assert status["content"]["tokens_total"] == 1234
    assert result["content"]["response"] == "done!"
    assert any(s["session"] == "default" and s["alive"] for s in sessions["content"]["sessions"])

    await session.stop()


async def test_abort_reports_dropped_queue(fake_manager, fake_channel):
    pi_mod = make_pi(fake_manager)
    _ctx, _loader, tc = await make_env(fake_channel, pi_mod)
    session, rpc = await attach_fake_session(pi_mod)
    session.busy = True

    tokens = await collect(tc, assistant_with_tools(
        tool_call("c1", "pi_abort", {"session": "default"})))
    payload = tool_payloads(tokens)[0]

    assert payload["status"] == "success"
    assert payload["content"]["status"] == "aborted"
    assert payload["content"]["dropped_queued_messages"] == ["mid-task redirect"]
    assert [c for c, _ in rpc.calls] == ["clear_queue", "abort"]

    await session.stop()


async def test_set_model_splits_provider_and_sets_thinking(fake_manager, fake_channel):
    pi_mod = make_pi(fake_manager)
    _ctx, _loader, tc = await make_env(fake_channel, pi_mod)
    session, rpc = await attach_fake_session(pi_mod)

    tokens = await collect(tc, assistant_with_tools(
        tool_call("c1", "pi_set_model",
                  {"model": "other/big-model", "thinking_level": "high"})))
    payload = tool_payloads(tokens)[0]

    assert payload["status"] == "success"
    assert ("set_model", {"model_id": "big-model", "provider": "other"}) in rpc.calls
    assert ("set_thinking_level", {"level": "high"}) in rpc.calls

    await session.stop()


async def test_send_queues_when_busy(fake_manager, fake_channel):
    pi_mod = make_pi(fake_manager)
    _ctx, _loader, tc = await make_env(fake_channel, pi_mod)
    session, rpc = await attach_fake_session(pi_mod)
    session.busy = True

    tokens = await collect(tc, assistant_with_tools(
        tool_call("c1", "pi_send", {"session": "default", "message": "also fix docs"})))
    payload = tool_payloads(tokens)[0]

    assert payload["content"]["status"] == "queued"
    assert ("follow_up", {"message": "also fix docs"}) in rpc.calls

    await session.stop()


# ---------------------------------------------------------------------------
# live pi (opt-in): the real agent behind the same pipeline
# ---------------------------------------------------------------------------

needs_live = pytest.mark.skipif(
    not os.environ.get("PI_LIVE"),
    reason="set PI_LIVE=1 to drive a real, authenticated pi through the pipeline "
           "(PI_LIVE_ARGS optionally holds a JSON list of pi CLI args)",
)


@needs_live
@pytest.mark.integration
async def test_real_pi_run_through_pipeline(fake_manager, fake_channel, tmp_path):
    args = json.loads(os.environ.get("PI_LIVE_ARGS", "[]"))
    pi_mod = make_pi(fake_manager, extra_args=args)
    if pi_mod.binary is None:
        pytest.skip("no pi binary found")

    _ctx, _loader, tc = await make_env(fake_channel, pi_mod)
    (tmp_path / "secret.txt").write_text("the magic word is marmalade")

    tokens = await collect(tc, assistant_with_tools(
        tool_call("c1", "pi_run", {
            "task": "Read secret.txt in your working folder and reply with just the magic word.",
            "cwd": str(tmp_path),
        })))

    payload = tool_payloads(tokens)[0]
    assert payload["status"] == "success", payload
    assert "marmalade" in payload["content"]["response"]
    assert "read" in payload["content"]["tools_used"]

    await pi_mod.on_shutdown()
