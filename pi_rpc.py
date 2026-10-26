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
JSONL RPC client for `pi --mode rpc`.

This speaks pi's documented RPC protocol (docs/rpc.md in the pi package):
one JSON object per line into the child's stdin, response lines
({type: "response"} with a matching id) resolve pending commands, and every
other line is an agent event that goes to the event queue for the session
pump to consume.

Framing note from the protocol docs: LF is the only record delimiter, and
asyncio's StreamReader splits on LF only, so readline() is protocol-compliant
(unlike Node's readline, which also breaks on U+2028/U+2029).
"""

import asyncio
import json
import os

# pi can print very large tool results; give the stream reader plenty of headroom
READER_LIMIT = 64 * 1024 * 1024

# how many stderr lines to keep around for error reports
STDERR_TAIL_LINES = 40


class PiRPCError(Exception):
    """a command came back with success: false, or the process died mid-command"""
    pass


class PiRPCClient:
    """
    controls one `pi --mode rpc` subprocess over stdin/stdout JSONL
    """

    def __init__(self, binary, cwd=None, extra_args=None, log=None):
        self.binary = binary
        self.cwd = cwd
        self.extra_args = list(extra_args or [])
        self._log = log or (lambda category, message: None)

        self.process = None
        self.alive = False
        self.stderr_tail = []

        self._events = asyncio.Queue()
        self._pending = {}  # response id -> Future
        self._next_id = 0
        self._readers = []

    # ------------------------- lifecycle -------------------------

    async def start(self, startup_args=None):
        """spawn `pi --mode rpc` and start draining its stdout/stderr.

        startup_args are appended after the configured extra_args (used for
        per-session flags like `--session <path>` and `--name <name>`).
        """
        args = [self.binary, "--mode", "rpc"] + self.extra_args + list(startup_args or [])
        env = os.environ.copy()
        # keep pi out of any interactive pager/locale assumptions; JSONL only
        env.setdefault("CI", "1")

        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                cwd=self.cwd or None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=READER_LIMIT
            )
        except (OSError, ValueError) as e:
            raise PiRPCError(f"could not start pi ({e})")

        self.alive = True
        self.stderr_tail = []
        self._readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]

    async def stop(self):
        """close stdin, then terminate (and finally kill) the process"""
        self.alive = False
        proc = self.process
        self.process = None

        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass

        for task in self._readers:
            task.cancel()
        self._readers = []

        self._fail_pending("the pi process was stopped")

    # ------------------------- io plumbing -------------------------

    async def _read_stdout(self):
        """read JSONL records from pi's stdout and dispatch them"""
        reader = self.process.stdout
        reason = "the pi process exited"

        while True:
            try:
                line = await reader.readline()
            except Exception as e:
                reason = f"reading pi's stdout failed ({e})"
                break

            if not line:
                break  # EOF: pi closed the pipe, i.e. it exited

            line = line.strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # not JSON: nothing sane to do with it

            if not isinstance(obj, dict):
                continue

            if obj.get("type") == "response":
                fut = self._pending.pop(str(obj.get("id")), None)
                if fut is not None and not fut.done():
                    fut.set_result(obj)
            else:
                self._events.put_nowait(obj)

        code = self.process.returncode
        if code is not None:
            reason += f" (exit code {code})"

        self.alive = False
        self._fail_pending(reason)
        self._events.put_nowait({"type": "__dead__", "reason": reason})

    async def _read_stderr(self):
        """keep a tail of pi's stderr around for diagnostics"""
        reader = self.process.stderr

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue

                self.stderr_tail.append(text)
                if len(self.stderr_tail) > STDERR_TAIL_LINES:
                    self.stderr_tail.pop(0)
        except (asyncio.CancelledError, Exception):
            pass

    def _fail_pending(self, reason):
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(PiRPCError(reason))
        self._pending = {}

    def stderr_summary(self):
        """last few stderr lines, for error reports (never parsed as protocol data)"""
        return "\n".join(self.stderr_tail[-10:])

    # ------------------------- commands -------------------------

    async def send(self, cmd: dict, timeout: float = 60.0):
        """
        send one command and wait for its correlated response.
        returns the response's data field, or raises PiRPCError
        """
        if not self.alive or self.process is None:
            raise PiRPCError("the pi process is not running")

        self._next_id += 1
        req_id = str(self._next_id)

        payload = dict(cmd)
        payload["id"] = req_id

        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        try:
            self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
        except Exception as e:
            self._pending.pop(req_id, None)
            raise PiRPCError(f"could not write to pi ({e})")

        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise PiRPCError(f"pi did not answer '{cmd.get('type')}' within {timeout}s")

        if not resp.get("success", False):
            raise PiRPCError(str(resp.get("error") or f"pi rejected '{cmd.get('type')}'"))

        return resp.get("data")

    async def next_event(self, timeout: float = None):
        """await the next agent event (raises asyncio.TimeoutError on timeout)"""
        if timeout is None:
            return await self._events.get()
        return await asyncio.wait_for(self._events.get(), timeout)

    # ------------------------- command wrappers -------------------------

    async def prompt(self, message, streaming_behavior=None, timeout: float = 60.0):
        cmd = {"type": "prompt", "message": message}
        if streaming_behavior:
            cmd["streamingBehavior"] = streaming_behavior
        return await self.send(cmd, timeout=timeout)

    async def steer(self, message):
        return await self.send({"type": "steer", "message": message})

    async def follow_up(self, message):
        return await self.send({"type": "follow_up", "message": message})

    async def abort(self):
        # abort waits until the session is idle again; give it room
        return await self.send({"type": "abort"}, timeout=60.0)

    async def clear_queue(self):
        return await self.send({"type": "clear_queue"}, timeout=15.0)

    async def get_state(self):
        return await self.send({"type": "get_state"}, timeout=15.0)

    async def new_session(self):
        return await self.send({"type": "new_session"})

    async def get_available_models(self):
        return await self.send({"type": "get_available_models"})

    async def set_model(self, model_id, provider=None):
        cmd = {"type": "set_model", "modelId": model_id}
        if provider:
            cmd["provider"] = provider
        return await self.send(cmd)

    async def set_thinking_level(self, level):
        return await self.send({"type": "set_thinking_level", "level": level})

    async def get_session_stats(self):
        return await self.send({"type": "get_session_stats"})

    async def get_last_assistant_text(self):
        return await self.send({"type": "get_last_assistant_text"})

    async def set_session_name(self, name):
        return await self.send({"type": "set_session_name", "name": name})
