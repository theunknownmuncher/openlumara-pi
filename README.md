# pi — the pi coding agent as a sidecar for OpenLumara's own agent

A user module for [OpenLumara](https://github.com/Rose22/openlumara) that gives your OpenLumara
agent a [pi coding agent](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) sidecar.

OpenLumara keeps running its own AI backend as usual. What the module adds is a set of `pi_*`
tools: your agent can delegate coding work to pi — a full coding agent with its own
read/bash/edit/write tools that operates directly on your filesystem — talk to it while it works,
and manage its sessions and models. OpenLumara's agent stays in the driver's seat throughout:
pi is a tool it delegates to, not a replacement for it.

## How it works

pi's SDK is a TypeScript in-process API; from Python the same surface is reached through pi's
documented language-independent equivalent: **RPC mode** (`pi --mode rpc`, JSONL commands,
responses and session events over stdin/stdout).

Each *named pi session* owns one `pi --mode rpc` subprocess, started on demand by the first tool
call that needs it and closed again by the idle reaper (or by `pi_close`). A background pump folds
pi's event stream into a run record (answer text, stop reason, tool calls so far), so tools can
report live progress. With `persist_sessions` on (default), each session's pi session file is
remembered in `data/pi_agent_sessions.json` and passed back via `pi --session <path>` when the
process restarts — so a session's conversation survives idle reaping, module reloads and
OpenLumara restarts.

## Tools

| Tool | What it does |
|---|---|
| `pi_run(task, session, cwd, background)` | Gives pi a coding task and waits for its answer (full tool activity summary included). `background=True` starts it and returns immediately — the tool-call level timeout stays out of your way for long tasks |
| `pi_send(session, message)` | Queues a message: a follow-up if pi is mid-task, a fresh run if idle. Never waits |
| `pi_get_result(session)` | Status + answer of the current/last run (pair with background runs) |
| `pi_steer(session, message)` | Redirects a running task mid-flight, after pi's current step |
| `pi_abort(session)` | Stops the current run and drops queued messages |
| `pi_status(session)` | Live state: model, busy/idle, message count, tokens/cost/context fill, current task progress |
| `pi_sessions()` | Lists known sessions: live or stopped, working folder, session file they resume from |
| `pi_close(session, forget)` | Stops a session's pi process; `forget=True` also resets the conversation |
| `pi_models(session)` | Lists the models pi has configured/credentials for |
| `pi_set_model(model, session, thinking_level)` | Switches a session's model (`provider/id` or bare id) and thinking level |

## Requirements

- OpenLumara — the module itself needs **no Python dependencies**
- **pi** installed and authenticated for at least one provider:

  ```bash
  npm install -g @earendil-works/pi-coding-agent
  ```

  Whatever pi has configured (its own OAuth/API keys, models, settings) is reused as-is; this
  module does not touch pi's configuration. If the pi binary cannot be found, the module stays
  passive: it logs a hint and the tools return an installation note instead of breaking.

## Setup

1. Put this folder into your OpenLumara `user_modules/` folder.
2. Restart OpenLumara and enable the module: `/module pi` (or the settings panel, or config).
3. Set `working_dir` to the project folder pi should default to (defaults to OpenLumara's folder).
4. Ask your agent to delegate something, e.g. *"have pi fix the failing tests in the api folder"*.

## Settings

| Setting | Default | Description |
|---|---|---|
| `pi_path` | `pi` | Path to the pi binary. Absolute, `~` and relative paths are used directly; bare names are looked up on `PATH`, then `~/.pi/agent/bin`, `~/.pi/bin`, `~/.local/bin` and friends |
| `working_dir` | `.` | Project folder pi sessions run in when a tool call doesn't pass an explicit `cwd` (where pi reads AGENTS.md/CLAUDE.md, edits files, runs commands) |
| `extra_args` | `[]` | Extra CLI arguments for every `pi --mode rpc` this module starts (one per entry, e.g. `--offline`). Don't pass `--mode`/`--session`, they're managed for you |
| `persist_sessions` | `true` | Remember each session's pi session file and resume it after restarts. Off = purely ephemeral sessions |
| `session_idle_timeout` | `900` | Seconds an idle session's pi process lives before being reaped (`0` = never). Restarted transparently on next use, resuming the conversation |
| `run_timeout` | `0` | Seconds `pi_run` waits before returning "still running" (`0` = unlimited). Remember OpenLumara's core `tool_timeout` also caps tool calls |
| `output_limit` | `0` | Max characters tool results may contain (`0` = unlimited) |
| `inject_instructions` | `true` | Injects a short usage guide for the `pi_*` tools into the system prompt |

## Limitations & notes

- pi reads, edits and executes shell commands **on the host, unsandboxed**, in the session's
  working folder. The module is marked `unsafe` for a reason: only enable it if you intend to give
  your agent (through pi) host access. Verify important changes pi makes.
- Each session is a full pi process (a Node.js instance). The idle reaper keeps their number in
  check; raise `session_idle_timeout` or `pi_close` sessions explicitly to tune this.
- pi's provider credentials and model choice are pi's own business (its config folder). Use
  `pi_models` / `pi_set_model` to switch within what pi already has access to.
- Extension dialogs (`extension_ui_request`) from pi extensions are ignored; pi's RPC mode
  degrades them the same way other non-TUI clients do.

## Tests

From the OpenLumara repo root (the suite reuses the repo's shared test harness and
path-isolating fixtures):

```bash
pytest user_modules/pi/tests
```

The fast tests drive the real OpenLumara pipeline (real `ToolLoader` registration + real
`ToolcallManager.process()` dispatch) with a scripted pi RPC layer, so they need no pi provider
credentials. To additionally drive a *real, authenticated* pi through the same pipeline, opt in:

```bash
PI_LIVE=1 pytest user_modules/pi/tests -k real_pi
```

(`PI_LIVE_ARGS` optionally holds a JSON list of extra pi CLI args, e.g. provider/model flags.)

## License

GPL-3.0-or-later (see `LICENSE`). pi itself is a separate project
(`@earendil-works/pi-coding-agent`) with its own license; this module only talks to it over
pi's public RPC protocol.
