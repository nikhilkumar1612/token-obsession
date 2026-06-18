"""MCP server definition."""

from mcp.server.fastmcp import FastMCP

from token_obsession.core.config import get_settings
from token_obsession.core.models import (
    Chain,
    CompareTokensResult,
    ExplainTokenResult,
    Opportunity,
    Strategy,
)
from token_obsession.services.scoring import TokenScoringService

settings = get_settings()
scoring_service = TokenScoringService(settings=settings)

mcp = FastMCP(
    name="token-obsession",
    instructions=(
        "Use this server to discover and explain Base token opportunities. "
        "Treat ranked outputs as strategy-specific signals, not guaranteed winners."
    ),
    json_response=True,
    streamable_http_path="/",
)


@mcp.tool()
def scan_tokens(
    strategy: Strategy,
    limit: int = 5,
    chain: Chain = Chain.BASE,
) -> list[Opportunity]:
    """Return ranked Base token opportunities for one strategy."""

    return scoring_service.scan_tokens(strategy=strategy, limit=limit, chain=chain)


@mcp.tool()
def explain_token(
    token_address: str,
    strategy: Strategy = Strategy.FRESH_QUALITY,
) -> ExplainTokenResult:
    """Explain why a Base token ranks the way it does."""

    return scoring_service.explain_token(token_address=token_address, strategy=strategy)


@mcp.tool()
def compare_tokens(
    token_addresses: list[str],
    strategy: Strategy = Strategy.SAFER_MOMENTUM,
) -> CompareTokensResult:
    """Compare several Base token candidates under one strategy."""

    return scoring_service.compare_tokens(
        token_addresses=token_addresses,
        strategy=strategy,
    )
