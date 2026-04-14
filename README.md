# Agentic PyMOL Plugin

**English** · [한국어](./README_ko.md)

Control PyMOL with natural language. Type a request in the PyMOL console and a
Gemini-powered DeepAgent plans the work, writes the PyMOL Python needed, and
runs it inside your live session.

```text
PyMOL> ask Show the ligand-protein interface of 2wyk as sticks
[agent] ready (server v0.1.0)
[agent] ▶ Show the ligand-protein interface of 2wyk as sticks
[main] → task(python_executor, "fetch 2wyk and identify the ligand")
[python_executor] → run_pymol_python(cmd.fetch('2wyk'))
[python_executor·run_pymol_python] ExecutiveLoad-Detail: Detected mmCIF
[python_executor] → run_pymol_python(cmd.select('lig', 'resn HEM'))
[python_executor] → run_pymol_python(cmd.show('sticks', 'byres lig around 5'))
[agent] ✓ Loaded 2wyk and displayed residues within 5 Å of the HEM ligand as sticks.
```

---

## Architecture at a glance

The plugin runs as **two separate Python processes**.

```
┌─────────────────────────────┐   ndjson over   ┌─────────────────────────────┐
│ PyMOL process (Python 3.10) │  stdin/stdout   │ Agent process (Python 3.11) │
│                             │ ◄─────────────► │                             │
│ • Plugin UI / commands      │  one JSON       │ • deepagents + LangChain    │
│ • run_pymol_python runner   │  object per     │ • Gemini (google-genai)     │
│ • AST safety checks         │  line           │ • .venv managed by uv       │
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
GOOGLE_API_KEY=AIzaSy...your_key_here...
```

### 5. Install the plugin into PyMOL

In PyMOL:

**Plugin → Plugin Manager → Install New Plugin → Choose file...**

- Select this project **as a directory**.
- PyMOL copies the plugin to `~/.pymol/startup/agentic_pymol_plugin/`.
- The Plugin Manager may skip hidden files (like `.env.local`) and the
  `agent/.venv/` folder. If they did not come along, copy them manually:
  ```bash
  cp .env.local ~/.pymol/startup/agentic_pymol_plugin/.env.local
  cp -R agent/.venv ~/.pymol/startup/agentic_pymol_plugin/agent/.venv
  ```
  Or, instead of copying, symlink the whole project into `~/.pymol/startup/`:
  ```bash
  ln -s ~/Projects/agentic_pymol_plugin ~/.pymol/startup/agentic_pymol_plugin
  ```
  With a symlink you edit the original source and PyMOL picks up the changes
  on its next start.

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
[main] → task(python_executor, "<sub-goal>")           ← planner delegates
[python_executor] → run_pymol_python(cmd.fetch...)     ← sub-agent runs code
[python_executor·run_pymol_python] <captured stdout>   ← tool output
[agent] ✓ <final summary>                              ← done
```

Stop in the middle with `ask_cancel`. Clear the conversation entirely with
`ask_reset`.

---

## Configuration

Tune behaviour through `.env.local`:

| Variable                      | Default             | Purpose                                                 |
| ----------------------------- | ------------------- | ------------------------------------------------------- |
| `GOOGLE_API_KEY`              | *(required)*        | Gemini API key.                                         |
| `GEMINI_API_KEY`              | —                   | Accepted as an alternative name for the key.            |
| `AGENTIC_PYMOL_MODEL`         | `gemini-2.5-flash`  | Model to use. Try `gemini-2.5-pro` for harder tasks.    |
| `AGENTIC_PYMOL_RECURSION`     | `50`                | LangGraph recursion limit.                              |
| `AGENTIC_PYMOL_TIMEOUT`       | `60`                | Per-call tool timeout in seconds.                       |
| `AGENTIC_PYMOL_AGENT_PYTHON`  | *(auto-detected)*   | Absolute path to the agent's Python, if you want to override the auto-resolved `agent/.venv/bin/python`. |

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

### Lots of `[agent-stderr] ...` lines appear

That is normal. The agent process writes its logging to stderr; the plugin
tags each line and forwards it to the PyMOL console. Useful while debugging.

### The agent seems stuck

```text
PyMOL> ask_cancel     # cancel only the current request
PyMOL> ask_reset      # restart the process (also clears memory)
```

### Live-edit workflow

Instead of reinstalling through the Plugin Manager, symlink the source
directory into PyMOL's startup folder:

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
                    │                  │  deepagents planner
                    │                  │      │
                    │                  │      ▼
                    │                  │  task(python_executor, "...")
                    │                  │      │
                    │                  │      ▼
                    │                  │  run_pymol_python(code)
                    │◄─────────────────┤  (RPC proxy)
                    │  tool_call       │
                    │                  │
                    │ AST check + exec │
                    │ inside PyMOL     │
                    │                  │
                    ├─────────────────►│
                    │  tool_result     │
                    │                  │
                    │◄─────────────────┤  final reply
                    │  event / done    │
                    ▼
              PyMOL console
```

### Components

- **`__init__.py`** — PyMOL plugin entry point. Registers `ask`, `ask_status`,
  `ask_cancel`, `ask_reset`.
- **`plugin_side/agent_client.py`** — Spawns the agent subprocess, reads ndjson
  from its stdout on a background thread, prints events to the PyMOL console,
  and fulfils `run_pymol_python` calls from the agent by executing them inside
  PyMOL.
- **`plugin_side/pymol_tools.py`** — Actually runs the generated code. Performs
  AST safety checks before calling `exec()`.
- **`plugin_side/safety.py`** — Blocks dangerous imports (`os`, `subprocess`,
  `shutil`, `sys`, `socket`, `urllib`, `requests`, ...) and destructive calls
  (`cmd.reinitialize()`, `cmd.delete('all')`, `cmd.quit()`, `open(..., 'w')`,
  ...). Regular commands like `cmd.fetch` are allowed.
- **`agent/` (uv project)** — Isolated 3.11 environment holding `deepagents`,
  `langchain-google-genai`, and `langgraph`. Nothing in here is imported by
  PyMOL.
- **`agent/src/agent_server/__main__.py`** — The ndjson message loop.
  Dispatches each incoming `request` to an `AgentRunner` and forwards the
  stream of events and tool calls back to stdout.
- **`agent/src/agent_server/remote_tool.py`** — A LangChain-compatible
  `run_pymol_python` tool that does not execute anything itself. It emits a
  `tool_call` message and blocks until a matching `tool_result` arrives from
  the plugin.

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

Sets the logging level for the agent process (written to stderr, surfaced in
PyMOL as `[agent-stderr] ...`).

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

(Fill in with the project's license of choice.)
