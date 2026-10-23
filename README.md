# atlas⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣

Entries for the Agentic AI hackathon⁤⁤⁣ (Oct 2026).

- **`torob/`** — in-store shopping assistant on the marketplace's⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ dataset → see [`torob/README.md`](torob/README.md) (Persian). Live: https://torob.frontage.store
- `core/` — small shared framework: OpenAI-compatible LLM client with failover, spend guard and FakeLLM (`core/llm.py`), event emitter + bounded tool loop (`core/agent_loop.py`), Persian text helpers (`core/text.py`).

```bash
uv sync
uv run python -m torob.etl.build_db --csv-dir path/to/torob/v16 --out data/torob.db
uv run uvicorn torob.app:app --port 8770
uv run pytest -q
```

**For judges:** the live app is at https://torob.frontage.store (the builder's own domain on a VPS in Iran, not an official the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ site). Recommendations, shops, distances and hours come from the marketplace's⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ dataset; shop names and phone numbers in the dataset are fake, so shop calls are simulated and labelled as such. The AI (Claude Sonnet 5.5) has a daily token cap; if it runs out, simpler rules answer instead. Step-by-step local setup (with or without AI, or with Docker) is in [`torob/README.md`](torob/README.md#راه‌اندازی-روی-سیستم-خودتان-حدود-۵-دقیقه).

The the marketplace⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ dataset CSVs are not committed (`products.csv` is 196 MB, over GitHub's limit); download them from the challenge page and build the DB with the command above.
