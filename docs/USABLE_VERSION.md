# Local acceptance and release boundaries

The usable development baseline is `feat/m7-m8-stack` (v0.8.1), not the
v0.6.1 `main` branch. M7 usage tracking and M8 adapters exist on that branch.
The rescue branch preserves an earlier working tree; it is not a second release.

## Install from source

```bash
git clone https://github.com/Wike-CHI/aer.git
cd aer
git checkout feat/usable-runtime
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,knowledge]'
python scripts/install_neug_extensions.py
python -m aer.demo --output-dir ./demo-results
```

Python 3.12+ and a platform supported by NeuG are required for this full scenario.
NeuG's wheel does not include FTS: the explicit extension installation needs
network access to the vendor's extension host. Docker installs it at build time.
An unavailable extension is an installation failure, not an empty knowledge store.

The demo creates a new directory on each invocation and never clears an existing
store. Without `--output-dir`, it uses temporary storage and cleans it after exit.
It exercises a failed tool, recovery, independent H1 verification, rule-based
distillation, real NeuG projection and retrieval, rendered-context consumption,
injection and adoption tracking, a second verified run, and usage after restart.
An incomplete scenario exits nonzero. Persistent runs include `evidence.json`.

This is a deterministic local simulation. It proves the SDK plumbing works; it
does not establish live WordPress correctness, model quality, or causal ROI.

## Development checks

```bash
pytest -q
ruff check .
ruff format --check .
mypy aer/
cd integrations/dsh
npm ci
AER_DSH_PYTHON=../../.venv/bin/python npm test
```

Set `AER_DSH_PYTHON` to the interpreter with AER installed. A bridge process using
an unrelated global Python cannot import the runtime.

## Boundaries before a production release

1. Merge and run remote CI for M7/M8 and this acceptance entry point; a development
   branch is not proof that PyPI or the production deployment contains these features.
2. Exercise a live model turn with Codex/DSH tools, independent outcome verification,
   session reopen, and replay. DSH's existing capture did not include authenticated
   model tool calls; fixture coverage must not be described as live acceptance.
3. Complete structured payload sanitization for the embedded SDK. The external
   adapter sanitizes inputs, but embedded callers remain responsible for secrets.
4. Run a real WordPress task with and without experience, measuring verified outcome,
   retries, duration and token usage. Current usage rates describe observations and
   cannot establish that experience caused improvement.

Keep SQLite as the fact source and NeuG as a rebuildable projection. New frameworks
should use the adapter protocol rather than changing the experience model. Add
embeddings or a dashboard only when task evidence identifies a need.
