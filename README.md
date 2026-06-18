# token-obsession

`token-obsession` is a Base-first MCP server for ranked token discovery.

This project currently gives us:

- a Python package managed by `uv`
- a FastAPI app that hosts a remote MCP server over Streamable HTTP
- three initial ranking strategies:
  - `fresh_quality`
  - `safer_momentum`
  - `high_greed_high_risk`
- GeckoTerminal-backed discovery for Base when a CoinGecko API key is configured
- DEX Screener enrichment for liquidity, pair activity, and boost context
- placeholder in-memory token data and scoring as a fallback when no live API key is set

## Quickstart

1. Install dependencies:

```bash
uv sync
```

2. If you want live GeckoTerminal data, set your API key:

```bash
export TOKEN_OBSESSION_COINGECKO_API_KEY=your_key_here
```

If you are using a CoinGecko Demo key, also set:

```bash
export TOKEN_OBSESSION_COINGECKO_BASE_URL=https://api.coingecko.com/api/v3
```

3. Start the app:

```bash
uv run uvicorn token_obsession.api.app:app --reload
```

4. Visit the health endpoint:

```bash
curl http://127.0.0.1:8000/health
```

5. Connect an MCP client to:

```text
http://127.0.0.1:8000/mcp
```

## Current MCP Tools

- `scan_tokens`
- `explain_token`
- `compare_tokens`

## Next Steps

- add security/risk enrichment after DEX Screener
- add discovery and enrichment workers
- persist canonical token, pool, and snapshot models
- tune scoring with real observations
