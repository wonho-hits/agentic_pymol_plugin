# agent-server

Out-of-process deep agent for the Agentic PyMOL Plugin.

Runs as a standalone Python 3.11 process managed by uv. The PyMOL plugin
(Python 3.10) spawns it as a subprocess and communicates over stdin/stdout
using newline-delimited JSON (ndjson).

## Setup

```bash
cd agent
uv sync
```

## Run manually (smoke test)

```bash
uv run agent-server < test_input.ndjson
```

Where each line of `test_input.ndjson` is one JSON message — see
`src/agent_server/protocol.py` for the message schema.

## Protocol

- **Plugin → Agent:** `request`, `tool_result`, `cancel`, `shutdown`
- **Agent → Plugin:** `ready`, `event`, `tool_call`, `done`, `error`

All messages carry an integer `id` that ties responses to the originating
request. `tool_call` additionally carries a `call_id` for tool-level
correlation.

## Tests

```bash
uv run pytest
```
