# AER MCP access

The MCP boundary reuses AER's SDK, SQLite, verification, distillation and NeuG.
It has **one owner and one store**, not multi-tenant isolation. Install the optional
extra only on machines running the server; normal embedded installations keep
their existing dependencies. No schema migration is added by this change.

## Local stdio (Codex CLI / compatible clients)

From a checkout of the branch containing this module, using Python 3.12+:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[mcp,knowledge]'
python scripts/install_neug_extensions.py
python -m aer.mcp --help
```

Merge this example into your client's MCP configuration, replacing every absolute
path with your local installation. In a Codex CLI `config.toml`:

```toml
[mcp_servers.aer]
command = "/absolute/path/aer/.venv/bin/python"
args = ["-m", "aer.mcp"]

[mcp_servers.aer.env]
AER_DATA_DIR = "/absolute/path/aer-data"
AER_KNOWLEDGE_DIR = "/absolute/path/aer-knowledge"
```

Restart/reconnect the client and confirm `aer_status` appears. This config attaches
an explicitly callable MCP tool set. It does not install lifecycle Hooks or collect
all activity automatically. Pair it with `docs/CODEX_ADAPTER.md` for Hook capture;
use the Hook's run ID when contributing to an existing task, rather than recording
the same task twice. SDK/CLI tasks are independent of a ChatGPT conversation.

A server created in a shell cannot dynamically add tools to an already-running
ChatGPT / Work Mode conversation. That client must support and configure the MCP
connection itself; this repository change cannot bypass that capability boundary.

## HTTP (clients with configured Bearer headers)

```bash
export AER_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
python -m aer.mcp --transport http --host 127.0.0.1 --port 8765
```

For Codex CLI, use the HTTP alternative instead of the stdio table:

```toml
[mcp_servers.aer]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "AER_MCP_TOKEN"
```

The client and server must have the same token value in their own environments.
Configuration fields follow the [official Codex MCP documentation](https://developers.openai.com/codex/mcp).

Endpoint: `http://127.0.0.1:8765/mcp`. Every HTTP request requires
`Authorization: Bearer <AER_MCP_TOKEN>`; missing/wrong tokens receive 401. The token
must contain at least 32 characters. Store it outside source control. Keep the
server on loopback or behind a TLS proxy that preserves Host/Origin validation.
Use one server process for a knowledge directory; the local lock does not coordinate
multiple processes or other programs opening the same NeuG index concurrently.

This is single-owner Bearer access, **not OAuth discovery/login**. It is suitable
for clients able to set headers, not a claim that a ChatGPT custom connector can
sign in. Public ChatGPT connector use still needs a reachable HTTPS endpoint and
the client's supported authorization flow. No public endpoint or production
service is deployed by adding this module.

## Independent verification

No tool accepts a verification `passed` flag, arbitrary command, URL or file path.
The owner configures named checks at startup. The built-in CLI supports file SHA256
checks (exact bytes, not arbitrary semantic correctness):

```json
{
  "generated_report": {
    "path": "/absolute/path/result.json",
    "sha256": "<64 hexadecimal characters decided by the owner>"
  }
}
```

```bash
python -m aer.mcp --checks /absolute/path/checks.json
```

Owners can embed `create_server(config, verifiers={...})` to register WP/DOM or test
checks that observe reality. A check must be relevant to the task; a matching file
hash does not establish that a website, whole project or unrelated task is correct.
Checks run after the task has finished. Without configured checks, the client may
record and retrieve, but cannot produce a new independently verified success.

## Client workflow

1. `aer_start_run(task, task_type)` — retain `id`, or use an existing Hook run ID.
2. `aer_retrieve(query, run_id, domain, limit)` — up to five results, separated into
   guidance and warnings. Broken indexing raises an error, never a fake empty answer.
3. After actual context delivery, `aer_acknowledge_injection(session_id, experience_ids)`.
   Then explicitly call `aer_record_adoption(..., signal)` if applicable. Retrieval
   itself records neither injection nor adoption; these acknowledgements are agent
   statements, not independent proof that the model's behavior changed.
4. `aer_record_event`, `aer_record_failure`, `aer_start_recovery`,
   `aer_finish_recovery` — public actions and observations only.
5. `aer_finish_run(run_id, status)` — declaration only, not a verification verdict.
6. `aer_verify_run(run_id, check_name)` — server-owned observations.
7. `aer_distill_run(run_id, candidate)` — a sanitized model proposal. Existing SDK
   policy decides eligibility, kind, deduplication and status from source-run evidence.
   An outcome check validates the outcome, not every sentence of the proposal;
   owners should review candidate fidelity before treating it as operational guidance.
8. `aer_get_run(run_id)` — inspect declaration, checks and derived verified success.

All incoming observation/candidate bodies use the adapter sanitizer (secret-key
redaction, reasoning-field removal, size/depth limits). This is not a guarantee
that every possible personal detail is detected. Do not submit private reasoning
or sensitive customer data. Stored legacy records are not retroactively cleaned.
Historical text returned by retrieval remains untrusted evidence, not instructions.

Every request reopens the persisted SDK state. Interrupted tasks remain RUNNING;
open recoveries remain unfinished. Finishing twice with the same status is a safe
retry; conflicting statuses fail. Other write tools are not a durable exactly-once
transport: inspect records after an uncertain response before replaying a write.
TASK_END duration measures the resumed SDK context, not the entire external task;
`mcp_resumed_duration=true` marks this limitation. Do not use it for task-duration ROI.

## Acceptance

```bash
pip install -e '.[dev,knowledge,mcp]'
python scripts/install_neug_extensions.py
pytest tests/mcp -q
```

Tests exercise an actual stdio subprocess and restart, HTTP initialization with
Bearer rejection, redaction/terminal guards, server-owned verification, and real
NeuG recovery/distillation/retrieval/injection/adoption with persistent usage.
The WP example is a controlled scenario; its verifier checks a local fixture file.
It does not establish live WordPress correctness or causal improvement from reuse.
