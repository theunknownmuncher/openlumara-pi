# pi module for OpenLumara — exposes the pi coding agent to the OpenLumara agent as tools
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# -- AI GENERATED CODE (OpenLumara agent, model Qwen3.8-Flash-Next) :: (2026-10-26) status:unreviewed
"""
Gives the OpenLumara agent a pi coding agent sidecar.

The pi coding agent (npm: @earendil-works/pi-coding-agent) is a full coding
agent with its own read/bash/edit/write tools, model registry and persistent
sessions. Its SDK is a TypeScript in-process API, so from Python the SDK
surface is reached through pi's documented language-independent equivalent:
pi's RPC mode (`pi --mode rpc`, JSONL commands + responses + session events).

This module keeps OpenLumara's own AI backend in charge and hands pi's SDK
features to it as tool calls: prompt/steer/follow-up/abort a run, keep named
sessions, inspect state and session stats, list and switch models. Each named
session owns one `pi --mode rpc` subprocess, started on demand and reaped
when idle. With persist_sessions on (default), each session's pi session file
is remembered, so a session survives process restarts (idle reaping, module
reload, OpenLumara restart) and is resumed with `pi --session <path>`.

See README.md for the tool reference and settings.
"""

import asyncio
import os
import re
import shutil
import time

import core

from .pi_rpc import PiRPCClient, PiRPCError

# session names double as filenames in pi's session folder, keep them tame
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DEFAULT_SESSION = "default"

# how often the idle-session reaper wakes up (seconds)
REAPER_INTERVAL = 30


class Pi(core.module.Module):
    """Gives the OpenLumara agent a pi coding agent sidecar: delegate coding tasks to pi through the pi_* tools (run, steer, abort, sessions, models). Pi runs as its own subprocess with its own read/bash/edit/write tools and keeps named, persistent conversations. Requires pi installed (npm install -g @earendil-works/pi-coding-agent); until the binary is found the module stays passive."""

    category = "development"

    # pi reads, edits and executes shell commands on the host: be honest about it
    unsafe = True

    settings = {
        "pi_path": {
            "default": "pi",
            "description": "Path to the pi binary (the pi coding agent CLI). Absolute paths, '~' paths and relative paths are used directly; a bare name like 'pi' is looked up on PATH, then common locations like ~/.pi/agent/bin/pi and ~/.local/bin/pi. Applies to newly started pi sessions"
        },
        "working_dir": {
            "default": ".",
            "description": "The project folder pi sessions run in when a tool call doesn't pass an explicit cwd (where pi reads AGENTS.md / CLAUDE.md, edits files and runs commands). Relative paths resolve from OpenLumara's folder; '~' is expanded"
        },
        "extra_args": {
            "type": "list",
            "default": [],
            "description": "Extra command-line arguments passed to every `pi --mode rpc` this module starts, one per list entry (for example '--offline' or '--no-tools'). Don't add '--mode' or '--session', those are managed for you"
        },
        "persist_sessions": {
            "type": "boolean",
            "default": True,
            "description": "Remember each pi session's pi session file (in data/pi_agent_sessions.json) and resume it when the session's pi process restarts, so conversations survive idle reaping and OpenLumara restarts. Turn off for purely ephemeral sessions (nothing is written to disk by this module)"
        },
        "session_idle_timeout": {
            "default": 900,
            "description": "Seconds an idle pi session's subprocess stays alive before the reaper closes it (a later tool call restarts it, and with persist_sessions on, resumes where it left off). 0 means never reap: keep pi processes alive until OpenLumara shuts down"
        },
        "run_timeout": {
            "default": 0,
            "description": "Maximum amount of time (in seconds) pi_run waits for pi to finish before returning with 'still running' (the run continues in the background, collect it with pi_result). Default 0 means unlimited; note that OpenLumara's core 'tool_timeout' setting also caps tool calls at the framework level, so raise it there too for long coding tasks"
        },
        "output_limit": {
            "default": 0,
            "description": "Maximum amount of characters the pi tools return (pi's answer text in particular). Default 0 means unlimited; set a positive number as a context-window safety cap"
        },
        "inject_instructions": {
            "type": "boolean",
            "default": True,
            "description": "Injects a short usage guide for the pi_* tools into the system prompt, so the agent knows when and how to delegate coding work to pi"
        }
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.binary = self._resolve_binary()

        self.start_lock = asyncio.Lock()  # serializes launching pi processes

        # name -> PiSession for every session this instance has touched
        self.live_sessions = {}

        # name -> {"path": pi session file, "cwd": working dir, "updated": ts}
        self.session_store = core.storage.StorageDict("pi_agent_sessions", type="json")

        if self.binary is None:
            self.log("pi", "pi binary not found: install it with 'npm install -g @earendil-works/pi-coding-agent' or set the pi_path setting. The pi_* tools stay passive until then.")

    # ------------------------- setup helpers -------------------------

    def _resolve_binary(self):
        """find the pi executable from the pi_path setting (explicit path wins, bare names get PATH + common locations)"""
        configured = str(self.config.get("pi_path") or "pi").strip() or "pi"
        expanded = os.path.expanduser(configured)

        # an explicit path wins, even when it is relative or lacks a separator
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded

        # a path-like config must not silently resolve to a different pi binary
        if configured != "pi" and (os.sep in expanded or (os.altsep and os.altsep in expanded)):
            self.log("pi", f"configured pi_path '{configured}' is not an executable file")
            return None

        found = shutil.which(expanded)
        if found:
            return found

        if expanded == "pi":
            for candidate in ("~/.pi/agent/bin/pi", "~/.pi/bin/pi", "~/.local/bin/pi", "~/.npm-global/bin/pi", "/usr/local/bin/pi", "/usr/bin/pi"):
                path = os.path.expanduser(candidate)
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    return path

        if configured != "pi":
            self.log("pi", f"configured pi_path '{configured}' was not found")

        return None

    def _resolve_cwd(self, cwd=""):
        """resolve a (possibly empty) working dir argument to an absolute path, relative paths from the working_dir setting"""
        raw = str(cwd or "").strip()
        if not raw:
            raw = str(self.config.get("working_dir") or ".").strip() or "."
        raw = os.path.expanduser(raw)
        if not os.path.isabs(raw):
            raw = core.get_path(raw, sandbox=False)
        return os.path.normpath(raw)

    def _run_timeout(self):
        try:
            value = float(self.config.get("run_timeout", default=0))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _idle_timeout(self):
        try:
            value = float(self.config.get("session_idle_timeout", default=900))
        except (TypeError, ValueError):
            return 900
        return value if value > 0 else None

    def _limit(self):
        try:
            value = int(self.config.get("output_limit", default=0))
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    def _limit_text(self, text):
        limit = self._limit()
        if limit and text and len(text) > limit:
            return text[:limit] + f"\n[... truncated at {limit} characters by the pi module's output_limit setting]"
        return text

    # ------------------------- session management -------------------------

    def _validate_name(self, name):
        name = str(name or "").strip() or DEFAULT_SESSION
        if not NAME_RE.match(name):
            return None, f"invalid session name '{name}': use letters, numbers, dots, dashes, underscores (max 64 chars)"
        return name, None

    def resume_path(self, name):
        """the pi session file to resume this session from, or None"""
        if not self.config.get("persist_sessions", default=True):
            return None
        entry = self.session_store.get(name)
        if not isinstance(entry, dict):
            return None
        path = entry.get("path") or ""
        return path if path and os.path.isfile(path) else None

    def _remember(self, name, cwd, path=None):
        """record the session's working dir and (when known) pi session file"""
        if not self.config.get("persist_sessions", default=True):
            return
        entry = dict(self.session_store.get(name) or {})
        entry["cwd"] = cwd
        if path:
            entry["path"] = path
        entry["updated"] = int(time.time())
        self.session_store[name] = entry

    async def _forget(self, name):
        if name in self.session_store:
            del self.session_store[name]

    async def _get_session(self, name, cwd=""):
        """return the live PiSession for name, starting its pi process if needed. raises PiRPCError with a human-readable message on failure"""
        if self.binary is None:
            raise PiRPCError("pi binary not found. Install it with 'npm install -g @earendil-works/pi-coding-agent' or set the pi_path setting.")

        session = self.live_sessions.get(name)
        if session is not None and session.alive:
            return session

        async with self.start_lock:
            # re-check: another tool call may have started it while we waited
            session = self.live_sessions.get(name)
            if session is not None and session.alive:
                return session

            if session is None:
                session = PiSession(self, name, self._resolve_cwd(cwd))
                self.live_sessions[name] = session
            else:
                await session.stop()  # tidy up leftovers (pump task) of the dead process

            extra_args = self.config.get("extra_args", default=[]) or []

            # a stale/renamed session file makes `pi --session` fail at startup:
            # try resuming first, then fall back to a fresh session once
            resume = self.resume_path(name)
            attempts = [resume, None] if resume else [None]

            last_error = None
            for attempt_resume in attempts:
                startup_args = ["--name", name]
                if attempt_resume:
                    startup_args = ["--session", attempt_resume] + startup_args

                session.rpc = PiRPCClient(self.binary, cwd=session.cwd, extra_args=extra_args, log=self.log)
                try:
                    await session.rpc.start(startup_args=startup_args)
                except PiRPCError as e:
                    last_error = e
                    if attempt_resume:
                        self.log("pi", f"could not resume session '{name}' from {attempt_resume}, starting fresh instead")
                    continue

                session.session_file = attempt_resume
                session.pump = asyncio.create_task(session._pump(), name=f"pi-pump-{name}")
                self._remember(name, session.cwd, attempt_resume)
                self.log("pi", f"session '{name}' started ({'resuming ' + attempt_resume if attempt_resume else 'fresh session'}, cwd {session.cwd})")
                return session

            self.live_sessions.pop(name, None)
            raise last_error if last_error else PiRPCError("could not start pi")

    def _drop_session(self, session, reason=""):
        """bookkeeping when a session's process died: keep the record and its session file so a later tool call can resume it"""
        if session.session_file:
            self._remember(session.name, session.cwd, session.session_file)
        if reason:
            self.log("pi", f"session '{session.name}' process died: {reason}")

    async def _close_session(self, name, reason="closed"):
        session = self.live_sessions.get(name)
        if session is None:
            return False
        await session.stop()
        await session.persist_state()
        self.live_sessions.pop(name, None)
        self.log("pi", f"session '{name}' {reason}")
        return True

    # ------------------------- run bookkeeping -------------------------

    def _begin_run(self, session, prompt):
        """start a new run record on the session (the pump folds events into it)"""
        run = {
            "prompt": prompt,
            "status": "running",
            "text": "",
            "stop_reason": None,
            "error": None,
            "tools": {},          # pi tool name -> call count
            "steers": [],
            "started": time.time(),
            "finished": None,
        }
        session.run = run
        session.last_activity = run["started"]
        return run

    def _run_summary(self, session, include_text=True):
        """compact, context-friendly view of a session's current/last run"""
        run = session.run
        if run is None:
            return {"session": session.name, "status": "idle", "note": "no run has happened on this session since its pi process started"}

        out = {
            "session": session.name,
            "status": run["status"],
            "prompt": run["prompt"][:300],
            "tools_used": run["tools"],
        }
        if run["finished"]:
            out["duration_seconds"] = int(run["finished"] - run["started"])
        if run["stop_reason"] and run["stop_reason"] not in ("stop", "end_turn"):
            out["stop_reason"] = run["stop_reason"]
        if run["error"]:
            out["error"] = run["error"]
        if run["steers"]:
            out["steers"] = [s[:120] for s in run["steers"]]
        if include_text and run["text"]:
            out["response"] = self._limit_text(run["text"])
        return out

    # ------------------------- lifecycle -------------------------

    async def on_ready(self):
        if self.binary:
            self.log("pi", f"pi coding agent available at {self.binary}. Delegate coding tasks with the pi_* tools.")

    async def on_shutdown(self):
        for name in list(self.live_sessions):
            await self._close_session(name, reason="closed (shutdown)")

    async def on_background(self):
        """reap pi processes of sessions that have been idle too long"""
        while True:
            await asyncio.sleep(REAPER_INTERVAL)

            idle_timeout = self._idle_timeout()
            if idle_timeout is None:
                continue

            now = time.time()
            for name, session in list(self.live_sessions.items()):
                if session.busy or not session.alive:
                    continue
                if now - session.last_activity < idle_timeout:
                    continue
                self.log("pi", f"session '{name}' idle for over {int(idle_timeout)}s, closing its pi process")
                await self._close_session(name, reason="closed (idle)")

    async def on_system_prompt(self):
        if not self.config.get("inject_instructions", default=True):
            return None

        note = "" if self.binary else " (the pi binary was not found, so the tools will report an installation hint until it is installed)"

        return f"""
# pi coding agent sidecar{note}
You can delegate coding work to pi, a full coding agent running as a sidecar (it has its own read/bash/edit/write/shell tools and remembers each named session's conversation):
- pi_run: hand it a self-contained coding task. Long tasks: background=True, then collect with pi_result.
- pi_steer redirects a running task, pi_send queues a message for later, pi_abort stops one, pi_status shows progress, pi_get_result collects output.
- pi_sessions / pi_close manage sessions (pi_run defaults to one called 'default'; use separate names for unrelated work). pi_models / pi_set_model control the model.
- pi edits files and runs shell commands on your real system inside its working folder: delegate build/test/refactor work, then verify important changes yourself.
"""

    # ------------------------- tools -------------------------

    @core.module.tool
    async def run(self, task: str, session: str = DEFAULT_SESSION, cwd: str = "", background: bool = False):
        """Runs a coding task on the pi coding agent and waits for its answer. pi is a full coding agent (its own read/bash/edit/write/shell tools) working in the given project folder; give it a self-contained task like 'fix the failing tests in X' or 'add feature Y'. Each named session is a continuing conversation, so follow-ups can reference earlier work. Set background=True for long tasks: returns immediately, then use pi_get_result to collect, pi_status to watch, or pi_steer to redirect while it runs.

        Args:
        task (str): The coding task/instructions for pi
        session (str): Named pi session to use (default 'default'); same name = same conversation, unrelated tasks should use separate names
        cwd (str): Optional project folder for a brand-new session (relative paths resolve from the module's working_dir setting). An already-running session keeps its folder
        background (bool): Start the task and return immediately instead of waiting for pi to finish
        """
        name, err = self._validate_name(session)
        if err:
            return self.result(err, success=False)

        try:
            sess = await self._get_session(name, cwd)
        except PiRPCError as e:
            return self.result(str(e), success=False)

        async with sess.start_lock:
            if sess.busy:
                return self.result(
                    f"session '{name}' is already running a task. Use pi_steer to redirect it, pi_send to queue a follow-up, or pi_abort to stop it. pi_status shows progress.",
                    success=False
                )

            run = self._begin_run(sess, task)
            waiter = sess.wait_for_settle()

            try:
                data = await sess.rpc.prompt(task)
            except PiRPCError as e:
                sess._cancel_waiter(waiter)
                run["status"] = "error"
                run["error"] = str(e)
                return self.result(f"pi rejected the task: {e}", success=False)

            if (data or {}).get("disposition") == "handled":
                sess._cancel_waiter(waiter)
                run["status"] = "completed"
                run["finished"] = time.time()
                return self.result({"session": name, "status": "handled", "note": "pi handled the prompt itself (extension command); no agent run started"})

            if background:
                watcher = asyncio.create_task(self._await_run(sess, waiter), name=f"pi-run-{name}")
                watcher.add_done_callback(self._watcher_report(name))
                return self.result({
                    "session": name,
                    "status": "started",
                    "note": "pi is working on it in the background. Use pi_status to check progress and pi_get_result to get the answer (pi_abort stops it).",
                })

        # foreground: wait outside the lock so other tools (status/steer/abort) stay usable
        try:
            await self._await_run(sess, waiter)
        except asyncio.TimeoutError:
            return self.result({
                "session": name,
                "status": "running",
                "note": f"pi is still working (run_timeout of {self.config.get('run_timeout', default=0)}s passed). The task continues in the background: use pi_get_result to collect it, pi_status to watch, or pi_abort to stop it.",
            })
        except PiRPCError as e:
            return self.result({**self._run_summary(sess, include_text=False), "error": str(e)}, success=False)

        summary = self._run_summary(sess)
        return self.result(summary, success=sess.run["status"] == "completed")

    @core.module.tool
    async def send(self, session: str, message: str):
        """Sends a message to a pi session without waiting for the answer: if pi is mid-task it is queued as a follow-up (handled after the current run), otherwise a new run is started immediately. Use pi_get_result to collect the answer afterwards. For following up on the default session, see pi_run's background mode too.

        Args:
        session (str): The pi session to message
        message (str): The message/prompt for pi
        """
        name, err = self._validate_name(session)
        if err:
            return self.result(err, success=False)

        try:
            sess = await self._get_session(name)
        except PiRPCError as e:
            return self.result(str(e), success=False)

        async with sess.start_lock:
            was_busy = sess.busy
            run = self._begin_run(sess, message)
            waiter = sess.wait_for_settle()

            try:
                if was_busy:
                    data = await sess.rpc.follow_up(message)
                else:
                    try:
                        data = await sess.rpc.prompt(message)
                    except PiRPCError:
                        # the session started streaming between the busy check and
                        # the prompt: queue as a follow-up instead of racing it
                        data = await sess.rpc.prompt(message, streaming_behavior="followUp")
            except PiRPCError as e:
                sess._cancel_waiter(waiter)
                run["status"] = "error"
                run["error"] = str(e)
                return self.result(f"pi rejected the message: {e}", success=False)

            if (data or {}).get("disposition") == "handled":
                sess._cancel_waiter(waiter)
                run["status"] = "completed"
                run["finished"] = time.time()
                return self.result({"session": name, "status": "handled", "note": "pi handled the message itself (extension command); no agent run started"})

            watcher = asyncio.create_task(self._await_run(sess, waiter), name=f"pi-send-{name}")
            watcher.add_done_callback(self._watcher_report(name))

        return self.result({
            "session": name,
            "status": "queued" if was_busy else "started",
            "note": "Use pi_get_result to get the answer when it's done; pi_status shows progress.",
        })

    # NOTE: named get_result, not result: the base Module.result() helper is
    # how every tool returns its payload, a tool called 'result' would clobber it
    @core.module.tool
    async def get_result(self, session: str = DEFAULT_SESSION, include_text: bool = True):
        """Returns the status and answer of the current or most recent run on a pi session (works after a background pi_run/pi_send): running, completed, aborted or error, plus pi's final answer text when it's there.

        Args:
        session (str): The pi session to report on
        include_text (bool): Include pi's answer text (turn off to fetch just the status/tool usage cheaply)
        """
        name, err = self._validate_name(session)
        if err:
            return self.result(err, success=False)

        sess = self.live_sessions.get(name)
        if sess is None:
            note = "its pi process is not running (it resumes on the next pi_run/pi_send" + (", from the remembered session file)" if self.resume_path(name) else ")")
            return self.result({"session": name, "status": "stopped", "note": f"session '{name}' is not active: {note}"})

        summary = self._run_summary(sess, include_text=include_text)
        if sess.run is None:
            summary["alive"] = sess.alive
        return self.result(summary, success=sess.run is not None and sess.run["status"] in ("running", "completed"))

    @core.module.tool
    async def steer(self, session: str, message: str):
        """Redirects a running pi task: the message reaches pi after its current step, before the next model call, so it can change direction mid-task without losing work (unlike pi_abort). Only works while the session is actually running something.

        Args:
        session (str): The running pi session to steer
        message (str): Instructions to inject into the running task
        """
        name, err = self._validate_name(session)
        if err:
            return self.result(err, success=False)

        sess = self.live_sessions.get(name)
        if sess is None or not sess.alive:
            return self.result(f"session '{name}' is not running. Start a task with pi_run first.", success=False)
        if not sess.busy:
            return self.result(f"session '{name}' is idle, nothing to steer. Queue a message with pi_send instead.", success=False)

        try:
            data = await sess.rpc.steer(message)
        except PiRPCError as e:
            return self.result(f"could not steer pi: {e}", success=False)

        if sess.run is not None:
            sess.run["steers"].append(message)
        sess.last_activity = time.time()
        return self.result({"session": name, "status": (data or {}).get("disposition", "queued"), "note": "pi will pick the steering message up after its current step"})

    @core.module.tool
    async def abort(self, session: str = DEFAULT_SESSION, clear_queue: bool = True):
        """Aborts the task a pi session is currently running and drops any queued follow-ups. Waits until pi is idle. Any remaining messages on the queue are reported so you can resend them if needed.

        Args:
        session (str): The pi session to abort
        clear_queue (bool): Also drop queued steering/follow-up messages instead of letting pi continue with them
        """
        name, err = self._validate_name(session)
        if err:
            return self.result(err, success=False)

        sess = self.live_sessions.get(name)
        if sess is None or not sess.alive:
            return self.result(f"session '{name}' is not running; nothing to abort.", success=False)

        cleared = {}
        if clear_queue:
            try:
                cleared = (await sess.rpc.clear_queue()) or {}
            except PiRPCError:
                pass

        try:
            await sess.rpc.abort()
        except PiRPCError as e:
            return self.result(f"could not abort pi: {e}", success=False)

        sess.last_activity = time.time()
        out = {"session": name, "status": "aborted"}
        dropped = (cleared.get("steering") or []) + (cleared.get("followUp") or [])
        if dropped:
            out["dropped_queued_messages"] = [m[:120] for m in dropped]
        return self.result(out)

    @core.module.tool
    async def status(self, session: str = DEFAULT_SESSION):
        """Shows a pi session's live state: pi's model, thinking level, whether it's mid-task, message count, token/cost/context-window usage, and the current or last task's progress (tool calls so far).

        Args:
        session (str): The pi session to inspect
        """
        name, err = self._validate_name(session)
        if err:
            return self.result(err, success=False)

        sess = self.live_sessions.get(name)
        if sess is None or not sess.alive:
            resumable = self.resume_path(name)
            return self.result({
                "session": name,
                "status": "stopped",
                "resumes_from": resumable or "a fresh session on next use",
            })

        out = {"session": name, "cwd": sess.cwd}
        try:
            state = await sess.rpc.get_state() or {}
        except PiRPCError as e:
            return self.result({"session": name, "status": "error", "error": str(e)}, success=False)

        model = state.get("model") or {}
        out["alive"] = True
        out["busy"] = sess.busy
        out["model"] = f"{model.get('provider', '?')}/{model.get('id', '?')}" if model else None
        out["thinking_level"] = state.get("thinkingLevel")
        out["message_count"] = state.get("messageCount")
        out["session_name"] = state.get("sessionName")
        out["session_file"] = state.get("sessionFile")
        if state.get("isCompacting"):
            out["compacting"] = True

        try:
            stats = await sess.rpc.get_session_stats() or {}
            usage = stats.get("contextUsage") or {}
            out["tokens_total"] = (stats.get("tokens") or {}).get("total")
            out["cost_usd"] = stats.get("cost")
            if usage:
                out["context_used_percent"] = usage.get("percent")
        except PiRPCError:
            pass  # stats are nice-to-have, state above is enough

        if sess.run is not None:
            out["run"] = self._run_summary(sess, include_text=False)
        return self.result(out)

    @core.module.tool
    async def sessions(self):
        """Lists all pi sessions this module knows about: name, whether the pi process is live, busy/idle, working folder, and the session file it resumes from after a restart."""
        out = []
        for name in sorted(set(self.live_sessions) | set(self.session_store.keys())):
            sess = self.live_sessions.get(name)
            entry = self.session_store.get(name) or {}
            out.append({
                "session": name,
                "alive": bool(sess and sess.alive),
                "busy": bool(sess and sess.busy),
                "cwd": (sess.cwd if sess else entry.get("cwd")),
                "last_task": (sess.run["prompt"][:100] if sess and sess.run else None),
                "resumes_from": entry.get("path"),
            })
        if not out:
            return self.result({"sessions": [], "note": "no pi sessions yet: pi_run starts one on first use"})
        return self.result({"sessions": out})

    @core.module.tool
    async def close(self, session: str = DEFAULT_SESSION, forget: bool = False):
        """Stops a pi session's process. With forget=True also forgets its pi session file, so the next pi_run on that name starts with a clean slate instead of resuming the conversation.

        Args:
        session (str): The pi session to close
        forget (bool): Also forget the saved session file (resets the conversation)
        """
        name, err = self._validate_name(session)
        if err:
            return self.result(err, success=False)

        closed = await self._close_session(name, reason="closed (by tool)")
        if forget:
            await self._forget(name)
        if not closed and not forget:
            return self.result(f"session '{name}' is not running.", success=False)
        return self.result({"session": name, "status": "closed", "forgotten": forget})

    @core.module.tool
    async def models(self, session: str = DEFAULT_SESSION):
        """Lists the models pi can use (provider/id strings, the ones pi has credentials configured for). Use one with pi_set_model.

        Args:
        session (str): The pi session whose model registry to query
        """
        name, err = self._validate_name(session)
        if err:
            return self.result(err, success=False)

        try:
            sess = await self._get_session(name)
        except PiRPCError as e:
            return self.result(str(e), success=False)

        try:
            data = await sess.rpc.get_available_models() or {}
        except PiRPCError as e:
            return self.result(f"could not get pi's model list: {e}", success=False)

        models = []
        for model in data.get("models") or data.get("availableModels") or []:
            if not isinstance(model, dict):
                continue
            # only the identifier: never echo credential/config fields back into context
            models.append(f"{model.get('provider', '?')}/{model.get('id', '?')}")
        models.sort()

        return self.result({"session": name, "count": len(models), "models": self._limit_text("\n".join(models)) if models else []})

    @core.module.tool
    async def set_model(self, model: str, session: str = DEFAULT_SESSION, thinking_level: str = ""):
        """Switches a pi session to a different model. 'model' takes provider/id (see pi_models) or a bare model id. Optionally set the thinking level (off, minimal, low, medium, high, xhigh, max).

        Args:
        model (str): The model to switch pi to, as 'provider/id' or a bare id
        session (str): The pi session to switch
        thinking_level (str): Optional thinking level: off/minimal/low/medium/high/xhigh/max
        """
        name, err = self._validate_name(session)
        if err:
            return self.result(err, success=False)

        try:
            sess = await self._get_session(name)
        except PiRPCError as e:
            return self.result(str(e), success=False)

        model_arg = str(model or "").strip()
        provider = None
        if "/" in model_arg:
            provider, model_arg = model_arg.split("/", 1)

        try:
            await sess.rpc.set_model(model_arg, provider=provider)
        except PiRPCError as e:
            return self.result(f"could not switch pi to model '{model}': {e}", success=False)

        out = {"session": name, "model": model}
        if thinking_level:
            try:
                await sess.rpc.set_thinking_level(thinking_level)
                out["thinking_level"] = thinking_level
            except PiRPCError as e:
                out["thinking_level_error"] = str(e)

        sess.last_activity = time.time()
        return self.result(out)

    # ------------------------- run awaiting -------------------------

    def _watcher_report(self, name):
        """done-callback that logs crashes of background run watchers instead of swallowing them"""
        def report(task):
            exc = task.exception()
            if exc is not None and not isinstance(exc, (asyncio.CancelledError, PiRPCError)):
                self.log("pi", f"background watcher for session '{name}' crashed: {core.detail_error(exc)}")
        return report

    async def _await_run(self, session, waiter):
        """wait for the session's run to settle (honoring run_timeout), then persist bookkeeping"""
        try:
            await asyncio.wait_for(waiter, self._run_timeout())
        except asyncio.TimeoutError:
            waiter.cancel()
            raise
        finally:
            session.last_activity = time.time()
            await session.persist_state()


class PiSession:
    """one named pi conversation: its `pi --mode rpc` process, the event pump
    that folds pi's session events into a run record, and settle waiters"""

    def __init__(self, module, name, cwd):
        self.module = module
        self.name = name
        self.cwd = cwd

        self.rpc = None
        self.pump = None
        self.session_file = None

        self.busy = False
        self.run = None
        self.last_activity = time.time()

        self.start_lock = asyncio.Lock()  # serializes starting runs
        self._waiters = []
        self._persist_task = None

    @property
    def alive(self):
        return self.rpc is not None and self.rpc.alive

    # ------------------------- event pump -------------------------

    async def _pump(self):
        """consume pi's event stream for this session's whole lifetime"""
        try:
            while True:
                event = await self.rpc.next_event()
                done = self._handle_event(event)
                if done:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._settle_waiters(PiRPCError("the pi session's process is gone"))

    def _handle_event(self, event):
        """fold one pi session event into the run record. returns True when the process died"""
        etype = event.get("type")

        if etype == "__dead__":
            reason = event.get("reason", "the pi process exited")
            stderr = self.rpc.stderr_summary() if self.rpc else ""
            if stderr:
                reason += f"\nlast pi stderr:\n{stderr}"
            if self.run is not None and self.run["status"] == "running":
                self.run["status"] = "error"
                self.run["error"] = reason
                self.run["finished"] = time.time()
            self.busy = False
            self.module._drop_session(self, reason=reason)
            return True

        elif etype == "agent_start":
            self.busy = True
            if self.run is not None:
                self.run["status"] = "running"

        elif etype == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant" and self.run is not None:
                content = message.get("content")
                if isinstance(content, list):
                    text = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
                    if text:
                        self.run["text"] = text  # last authoritative assistant text wins
                self.run["stop_reason"] = message.get("stopReason")
                if message.get("errorMessage"):
                    self.run["error"] = str(message["errorMessage"])

        elif etype == "tool_execution_start":
            if self.run is not None:
                tool_name = event.get("toolName", "?")
                self.run["tools"][tool_name] = self.run["tools"].get(tool_name, 0) + 1

        elif etype == "extension_error" and self.run is not None:
            self.run["error"] = self.run["error"] or f"pi extension error: {event.get('error')}"

        elif etype == "agent_settled":
            self.busy = False
            if self.run is not None and self.run["status"] == "running":
                if event.get("aborted"):
                    self.run["status"] = "aborted"
                    # pi reports aborted turns with stopReason "error"; show it honestly
                    if self.run["stop_reason"] == "error":
                        self.run["stop_reason"] = "aborted"
                elif self.run["stop_reason"] == "error":
                    self.run["status"] = "error"
                else:
                    self.run["status"] = "completed"
                self.run["finished"] = time.time()
            self.last_activity = time.time()
            self._settle_waiters()
            # remember the session file even when no tool is still awaiting
            # the run (timed-out foreground runs, background runs)
            self._persist_task = asyncio.create_task(self.persist_state())

        return False

    # ------------------------- settle waiters -------------------------

    def wait_for_settle(self):
        """a Future that resolves the next time pi reports agent_settled (or fails if the process dies first)"""
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        return fut

    def _cancel_waiter(self, fut):
        if fut in self._waiters:
            self._waiters.remove(fut)
        if not fut.done():
            fut.cancel()

    def _settle_waiters(self, exception=None):
        for fut in self._waiters:
            if fut.done():
                continue
            if exception is not None:
                fut.set_exception(exception)
            else:
                fut.set_result(None)
        self._waiters = []

    # ------------------------- process bookkeeping -------------------------

    async def persist_state(self):
        """remember pi's session file for this session so a restart can resume it"""
        if not self.alive:
            return
        try:
            state = await self.rpc.get_state() or {}
        except PiRPCError:
            return
        path = state.get("sessionFile")
        if path:
            self.session_file = path
            self.module._remember(self.name, self.cwd, path)

    async def stop(self):
        """terminate the pump and the pi process"""
        task, self.pump = self.pump, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        rpc, self.rpc = self.rpc, None
        self.busy = False
        if rpc is not None:
            await rpc.stop()
