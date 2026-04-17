# Agentic PyMOL Plugin

**English** · [한국어](./README_ko.md)

Control PyMOL with natural language. Type a request in the PyMOL console and a
Gemini-powered DeepAgent plans the work, writes the PyMOL Python needed, and
runs it inside your live session.

https://github.com/user-attachments/assets/30cf4690-fcac-4e4a-88c9-0d40a9d23f84

```text
PyMOL> ask Show the ligand-protein interface of 2wyk as sticks
[agent] ▶ Show the ligand-protein interface of 2wyk as sticks
[model] → run_pymol_python:
  cmd.fetch('2wyk', async_=0)
  cmd.select('lig', 'hetatm and not (resn HOH or solvent or inorganic)')
  cmd.show('sticks', 'byres polymer within 5 of lig')
  cmd.zoom('lig', 8)
[tool·run_pymol_python] OK
[agent] ✓ Loaded 2wyk and displayed residues within 5 Å of the ligand as sticks.
```

---

## Architecture at a glance

The plugin runs as **two separate Python processes**.

```
┌─────────────────────────────┐   ndjson over   ┌─────────────────────────────┐
│ PyMOL process (Python 3.10) │  stdin/stdout   │ Agent process (Python 3.11) │
│                             │ ◄─────────────► │                             │
│ • Plugin UI / commands      │  one JSON       │ • deepagents + LangChain    │
│ • Tool handlers (exec,      │  object per     │ • Gemini (google-genai)     │
│   mutate, screenshot, ...)  │  line           │ • Vision analysis (local)   │
│ • AST safety checks         │                 │ • .venv managed by uv       │
└─────────────────────────────┘                 └─────────────────────────────┘
```

Why split it?
- PyMOL typically ships with Python 3.10, while current LangChain / deepagents
  prefer 3.11+.
- Installing the heavy agent stack into PyMOL's interpreter pollutes that
  environment and often causes dependency conflicts.
- If the agent process crashes, PyMOL stays up.

See the [Architecture](#architecture) section for the full picture.

---

## Prerequisites

- **PyMOL** (Incentive or Open-Source). A recent build bundling Python 3.10 or
  newer is recommended.
- **uv** — installs and manages the agent's Python 3.11 environment for you.
  If you do not have it yet, install with:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  Open a new terminal (or `source ~/.zshrc` / `~/.bashrc`) so the new `uv`
  command is on your PATH.
- **Gemini API key** — get one at <https://aistudio.google.com/app/apikey>.

---

## Step-by-step setup

### 1. Get the source

Clone (or download) the repository into a location of your choice.

```bash
cd ~/Projects
git clone <your-fork-or-source> agentic_pymol_plugin
cd agentic_pymol_plugin
```

### 2. Install the agent environment (uv)

This creates the Python 3.11 virtualenv the agent process will run in. You only
need to do this once.

```bash
cd agent
uv sync
cd ..
```

The first run downloads a Python 3.11 interpreter and installs the dependencies
(`deepagents`, `langchain-google-genai`, `langgraph`, ...) into `agent/.venv/`.

> **Verify:** `ls agent/.venv/bin/python` should now show the interpreter.

### 3. Install the plugin-side dependency

The plugin is loaded inside PyMOL, so install the single lightweight dependency
into **the Python that PyMOL uses**.

Don't know which Python that is? Ask PyMOL:

```python
PyMOL> import sys; print(sys.executable)
```

Then install with that interpreter:

```bash
/path/to/pymol/python -m pip install -r requirements.txt
```

Using Open-Source PyMOL via conda?

```bash
conda activate <your-pymol-env>
pip install -r requirements.txt
```

### 4. Add your API key

```bash
cp .env.example .env.local
```

Open `.env.local` in your editor and replace `your_gemini_api_key_here` with
your actual key:

```dotenv
GOOGLE_API_KEY=your_key_here
```

### 5. Install the plugin into PyMOL

PyMOL's Plugin Manager only accepts `.py`, `.zip`, or `.tar.gz`, so build a
zip from the project root:

```bash
make plugin
```

This produces `dist/agentic_pymol_plugin.zip` containing just the in-process
plugin files (`__init__.py`, `config.py`, `plugin_side/`). The `agent/` uv
project stays where it is — it runs out-of-process and is not bundled in the
zip.

In PyMOL:

**Plugin → Plugin Manager → Install New Plugin → Choose file...**

- Select `dist/agentic_pymol_plugin.zip`.
- PyMOL extracts it into `~/.pymol/startup/agentic_pymol_plugin/`.
- The zip does **not** include `.env.local` or the `agent/` project. Wire
  them up after install:
  ```bash
  cp .env.local ~/.pymol/startup/agentic_pymol_plugin/.env.local
  ```
  Then point the plugin at the agent project with one of:
  - Set absolute paths in `.env.local`:
    ```dotenv
    AGENTIC_PYMOL_AGENT_PYTHON=/absolute/path/to/agent/.venv/bin/python
    AGENTIC_PYMOL_AGENT_DIR=/absolute/path/to/agent
    ```
    Setting `AGENTIC_PYMOL_AGENT_PYTHON` alone is enough — the plugin
    derives the agent directory from it. Set `AGENTIC_PYMOL_AGENT_DIR`
    explicitly if your layout doesn't follow `agent/.venv/bin/python`.
  - Or symlink the agent project next to the installed plugin:
    ```bash
    ln -s ~/Projects/agentic_pymol_plugin/agent \
          ~/.pymol/startup/agentic_pymol_plugin/agent
    ```

For live-edit development, skip the zip and symlink the whole source tree
into PyMOL's startup folder (see [Live-edit workflow](#live-edit-workflow)).

Restart PyMOL. The plugin loads automatically and registers the commands below.

### 6. First run

In the PyMOL console:

```text
PyMOL> ask Fetch 1ubq and show it as cartoon
```

The very first call starts the agent process in the background. You will see
`[agent] ready (server v0.1.0)`. Subsequent calls reuse the same process, so
they start responding immediately.

---

## Usage

Four commands are available directly in the PyMOL console.

| Command           | Description                                                  |
| ----------------- | ------------------------------------------------------------ |
| `ask <text>`      | Send a natural-language request to the agent.                |
| `ask_status`      | Show whether a request is currently in flight.               |
| `ask_cancel`      | Cancel the currently running request.                        |
| `ask_reset`       | Clear conversation memory (restarts the agent process).      |

### Example requests

Simple load and display:
```text
ask Fetch 1crn, show it as cartoon, and highlight hydrophobic residues in orange
```

Interaction analysis:
```text
ask Find polar residues within 5 Å of the ligand in 2wyk and list their names and distances
```

View control:
```text
ask Rotate the current selection around its center for one second
```

### Reading the progress log

While the agent works, the PyMOL console shows a stream like this:

```text
[agent] ▶ <your request>
[model] → run_pymol_python:                          ← agent calls a tool
  cmd.fetch('1ubq', async_=0)                        ← multi-line code shown indented
  cmd.show_as('cartoon', '1ubq')
[tool·run_pymol_python] OK                           ← tool output (short = one line)
[agent] ✓ Fetched 1ubq and displayed as cartoon.     ← final reply
```

For complex tasks, the agent may delegate to a sub-agent via `task()`:

```text
[model] → task(python_executor, "<sub-goal>")
```

Stop in the middle with `ask_cancel`. Clear the conversation entirely with
`ask_reset`.

### Available tools

The agent can call the following tools. Most are executed on the PyMOL side
via the ndjson bridge; `describe_viewport` chains a remote screenshot with a
local Gemini Vision call.

| Tool | Runs on | Description |
| ---- | ------- | ----------- |
| `run_pymol_python(code)` | PyMOL | Execute arbitrary Python inside the live session. |
| `inspect_session()` | PyMOL | Return a JSON snapshot of loaded objects, chains, ligand groups, and selections. |
| `mutate_residue(obj, chain, resi, target_aa)` | PyMOL | Safely mutate a single residue via the mutagenesis wizard. |
| `capture_viewport()` | PyMOL | Save a screenshot of the current viewport to a temp file. |
| `describe_viewport()` | Agent (local) | Capture the viewport and return a natural-language description via Gemini Vision. |

The main agent has access to all tools plus `task()` (sub-agent delegation)
and `write_todos()`. The python executor sub-agent has all tools except
`describe_viewport`.

---

## Configuration

Tune behaviour through `.env.local`:

| Variable                      | Default             | Purpose                                                 |
| ----------------------------- | ------------------- | ------------------------------------------------------- |
| `GOOGLE_API_KEY`              | *(required)*        | Gemini API key.                                         |
| `GEMINI_API_KEY`              | —                   | Accepted as an alternative name for the key.            |
| `AGENTIC_PYMOL_MODEL`         | `gemini-3-flash-preview` | Model to use. Fall back to `gemini-2.5-flash` if the preview is rate-limited; `gemini-2.5-flash-lite` for speed/cost on trivial workloads; `gemini-2.5-pro` for harder tasks. |
| `AGENTIC_PYMOL_RECURSION`     | `50`                | LangGraph recursion limit.                              |
| `AGENTIC_PYMOL_TIMEOUT`       | `60`                | Per-call tool timeout in seconds.                       |
| `AGENTIC_PYMOL_HISTORY_TURNS` | `10`                | Number of recent conversation turns kept in context. Older turns are discarded to limit token usage. |
| `AGENTIC_PYMOL_AGENT_PYTHON`  | *(auto-detected)*   | Absolute path to the agent's Python, if you want to override the auto-resolved `agent/.venv/bin/python`. The agent project root is then derived from this path. |
| `AGENTIC_PYMOL_AGENT_DIR`     | *(auto-derived)*    | Absolute path to the `agent/` project root. Required when the plugin was installed via the Plugin Manager (zip), since that copy does not include the `agent/` folder. |

Most users only need `GOOGLE_API_KEY`.

---

## Troubleshooting

### `[agent] failed to start agent subprocess: agent python not found`

The `agent/.venv/` is missing or broken. From the project root:
```bash
cd agent && uv sync
```

### `[agent] config error: GOOGLE_API_KEY not set`

`.env.local` is missing from the installed plugin directory, or the key is
empty. Check `~/.pymol/startup/agentic_pymol_plugin/.env.local`.

### `[agent] failed to start agent subprocess: ... No such file or directory: '.../agentic_pymol_plugin/agent'`

You installed via the zip, which does not bundle the `agent/` project. Set
`AGENTIC_PYMOL_AGENT_PYTHON` (or `AGENTIC_PYMOL_AGENT_DIR`) in
`.env.local`, or symlink your source `agent/` next to the installed plugin
(see step 5).

### Where are the agent logs?

The agent subprocess writes its logging to stderr, which the plugin
redirects to a file instead of the PyMOL console. The path is shown on
startup:

```text
[agent] ready — model=... thread=... (stderr → .../agentic_pymol_plugin/agent.log)
```

Tail it in a separate terminal while debugging:

```bash
tail -f ~/.pymol/startup/agentic_pymol_plugin/agent.log
```

Each session is separated by a `--- agent-stderr session <id> ---` header.

### The agent seems stuck

```text
PyMOL> ask_cancel     # cancel only the current request
PyMOL> ask_reset      # restart the process (also clears memory)
```

### Live-edit workflow

Instead of rebuilding the zip through the Plugin Manager on every change,
symlink the source directory into PyMOL's startup folder:

```bash
ln -s ~/Projects/agentic_pymol_plugin ~/.pymol/startup/agentic_pymol_plugin
```

Edits to the source take effect the next time PyMOL starts.

---

## Architecture

```
ask "..."  ─►  AgentClient ─► subprocess (agent-server)
                    │                  │
                    │  ndjson request  │
                    ├─────────────────►│
                    │                  │  Main Agent (Gemini)
                    │                  │      │
                    │                  │      ├─ trivial → run_pymol_python
                    │                  │      ├─ query   → inspect_session
                    │                  │      ├─ mutate  → mutate_residue
                    │                  │      ├─ vision  → describe_viewport
                    │                  │      │             (local: capture + Gemini Vision)
                    │                  │      └─ complex → task(python_executor)
                    │                  │
                    │◄─────────────────┤  tool_call (remote tools)
                    │                  │
                    │  PyMOL handler:  │
                    │  exec / snapshot │
                    │  mutate / png    │
                    │                  │
                    ├─────────────────►│
                    │  tool_result     │
                    │                  │
                    │◄─────────────────┤  event / done
                    ▼
              PyMOL console
```

### Components

- **`__init__.py`** — PyMOL plugin entry point. Registers `ask`, `ask_status`,
  `ask_cancel`, `ask_reset`.
- **`plugin_side/agent_client.py`** — Spawns the agent subprocess, reads ndjson
  from its stdout on a background thread, renders events to the PyMOL console,
  and dispatches tool calls from the agent to the appropriate handler.
- **`plugin_side/pymol_tools.py`** — Tool handlers that run inside PyMOL:
  `run_pymol_python` (AST-checked `exec()`), `inspect_session` (structured
  JSON snapshot), `mutate_residue` (safe mutagenesis wizard wrapper), and
  `capture_viewport` (screenshot to temp file).
- **`plugin_side/safety.py`** — Blocks dangerous imports (`os`, `subprocess`,
  `shutil`, `sys`, `socket`, `urllib`, `requests`, ...) and destructive calls
  (`cmd.reinitialize()`, `cmd.delete('all')`, `cmd.quit()`, `open(..., 'w')`,
  ...). Regular commands like `cmd.fetch` are allowed.
- **`agent/` (uv project)** — Isolated 3.11 environment holding `deepagents`,
  `langchain-google-genai`, and `langgraph`. Nothing in here is imported by
  PyMOL.
- **`agent/src/agent_server/__main__.py`** — The ndjson message loop.
  Dispatches each incoming `request` to an `AgentRunner`, manages conversation
  history (with configurable turn cap), and forwards events and tool calls
  back to stdout.
- **`agent/src/agent_server/remote_tool.py`** — LangChain tool bindings. Remote
  tools (`run_pymol_python`, `inspect_session`, `mutate_residue`) emit a
  `tool_call` message and block until a matching `tool_result` arrives.
  `describe_viewport` is a local tool that chains a remote screenshot capture
  with a Gemini Vision API call.

### Message protocol

Every line on the pipe is one JSON object (ndjson).

- **Plugin → Agent:** `request`, `tool_result`, `cancel`, `shutdown`
- **Agent → Plugin:** `ready`, `event`, `tool_call`, `done`, `error`

The schema lives in `agent/src/agent_server/protocol.py`. The plugin keeps its
own copy in `plugin_side/protocol.py` so it does not depend on the agent's
environment. `tests/test_protocol_parity.py` guards against drift between the
two copies.

---

## Developer guide

### Build the installable zip

```bash
make plugin     # → dist/agentic_pymol_plugin.zip
make clean      # remove dist/
```

The Makefile copies only the plugin-side files into `dist/build/` and zips
that staging directory, stripping `__pycache__` and `.DS_Store`.

### Running tests

Plugin side (no PyMOL needed — uses the agent's Python for convenience):
```bash
agent/.venv/bin/python -m pytest tests/ -q
```

Agent side:
```bash
cd agent && uv run pytest -q
```

### Running the agent server standalone

Useful for debugging the protocol without spinning up PyMOL.

```bash
cd agent
uv run agent-server
```

Feed it ndjson messages on stdin (press `Ctrl-D` to end):

```json
{"type":"request","id":1,"prompt":"hello"}
```

Note that any `run_pymol_python` calls will time out, since only the PyMOL
plugin can actually execute them. This mode is for smoke-testing the message
flow.

### Log level

```bash
export AGENTIC_PYMOL_LOG=DEBUG
```

Sets the logging level for the agent process (written to `agent.log` next to
the installed plugin; see "Where are the agent logs?" above).

---

## Safety notes

This plugin **executes LLM-generated Python inside your live PyMOL session**.
The AST safety layer blocks the obvious footguns, but it is not a sandbox.
Please:

- Save your work before running risky requests: `cmd.save('backup.pse')`.
- If you see unexpected tool calls, stop with `ask_cancel` and review the log.
- `.env.local` contains your API key — make sure it is listed in `.gitignore`
  (it is by default) before committing.

---

## License

[MIT](./LICENSE)
