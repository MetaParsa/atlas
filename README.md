# starcoach-agents

Entries for the Agentic AI hackathon⁤⁤⁣ (Oct 2026).

- **`torob/`** — in-store shopping assistant on the marketplace's⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ dataset → see [`torob/README.md`](torob/README.md) (Persian). Live: https://torob.frontage.store
- `core/` — small shared framework: OpenAI-compatible LLM client with failover, spend guard and FakeLLM (`core/llm.py`), event emitter + bounded tool loop (`core/agent_loop.py`), Persian text helpers (`core/text.py`).

```bash
uv sync
uv run python -m torob.etl.build_db --csv-dir path/to/torob/v16 --out data/torob.db
uv run uvicorn torob.app:app --port 8770
uv run pytest -q
```

The the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ dataset CSVs are not committed (`products.csv` is 196 MB, over GitHub's limit); download them from the challenge page and build the DB with the command above.
