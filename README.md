# token-obsession

`token-obsession` is a Base-first MCP server for ranked token discovery.

This bootstrap gives us:

- a Python package managed by `uv`
- a FastAPI app that hosts a remote MCP server over Streamable HTTP
- three initial ranking strategies:
  - `fresh_quality`
  - `safer_momentum`
  - `high_greed_high_risk`
- placeholder in-memory token data and scoring so we can validate the shape before wiring real data sources

## Quickstart

1. Install dependencies:

```bash
uv sync
```

2. Start the app:

```bash
uv run uvicorn token_obsession.api.app:app --reload
```

3. Visit the health endpoint:

```bash
curl http://127.0.0.1:8000/health
```

4. Connect an MCP client to:

```text
http://127.0.0.1:8000/mcp
```

## Current MCP Tools

- `scan_tokens`
- `explain_token`
- `compare_tokens`

## Next Steps

- replace bootstrap sample data with real Base market data
- add discovery and enrichment workers
- persist canonical token, pool, and snapshot models
- tune scoring with real observations
