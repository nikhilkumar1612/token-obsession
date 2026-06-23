import json

import httpx
import pytest

from token_obsession.core.config import Settings
from token_obsession.services.uniswap import UniswapClient, UniswapClientError

FROM_TOKEN = "0xb20A4Bd059F5914a2F8B9c18881c637f79efb7df"
TO_TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WALLET = "0xD52432be369A9AE28DfaB1A32124c66a249A3799"
ROUTER = "0x1111111111111111111111111111111111111111"
AMOUNT = 10_000_000_000_000


def _approval_data(spender: str, amount: int) -> str:
    spender_word = spender.removeprefix("0x").lower().rjust(64, "0")
    amount_word = hex(amount)[2:].rjust(64, "0")
    return "0x095ea7b3" + spender_word + amount_word


def _quote_response() -> dict:
    return {
        "requestId": "request-1",
        "routing": "CLASSIC",
        "permitData": None,
        "permitTransaction": None,
        "quote": {
            "quoteId": "quote-1",
            "chainId": 8453,
            "tradeType": "EXACT_INPUT",
            "swapper": WALLET,
            "input": {"token": FROM_TOKEN, "amount": str(AMOUNT)},
            "output": {
                "token": TO_TOKEN,
                "amount": "39700000",
                "minimumAmount": "38707500",
                "recipient": WALLET,
            },
        },
    }


def _handler(*, approval_spender: str = ROUTER, quote_status: int = 200):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["x-permit2-disabled"] == "true"
        body = json.loads(request.content)

        if request.url.path == "/v1/check_approval":
            assert body["amount"] == str(AMOUNT)
            return httpx.Response(
                200,
                json={
                    "requestId": "approval-1",
                    "cancel": None,
                    "approval": {
                        "to": FROM_TOKEN,
                        "from": WALLET,
                        "data": _approval_data(approval_spender, AMOUNT),
                        "value": "0",
                        "chainId": 8453,
                    },
                },
            )
        if request.url.path == "/v1/quote":
            assert body["protocols"] == ["V2", "V3", "V4"]
            assert body["slippageTolerance"] == 2.5
            if quote_status != 200:
                return httpx.Response(quote_status, json={"message": "No quotes available"})
            return httpx.Response(200, json=_quote_response())
        if request.url.path == "/v1/swap":
            assert body["quote"] == _quote_response()["quote"]
            assert body["simulateTransaction"] is False
            return httpx.Response(
                200,
                json={
                    "requestId": "swap-1",
                    "swap": {
                        "to": ROUTER,
                        "from": WALLET,
                        "data": "0x1234",
                        "value": "0",
                        "chainId": 8453,
                    },
                },
            )
        raise AssertionError(f"Unexpected request path: {request.url.path}")

    return handler, requests


def _client(handler) -> UniswapClient:
    return UniswapClient(
        settings=Settings(uniswap_api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )


def test_builds_classic_swap_with_direct_approval() -> None:
    handler, requests = _handler()

    quote = _client(handler).get_swap_quote(
        from_token=FROM_TOKEN,
        to_token=TO_TOKEN,
        from_amount=AMOUNT,
        wallet_address=WALLET,
        slippage_percent=2.5,
    )

    assert [request.url.path for request in requests] == [
        "/v1/check_approval",
        "/v1/quote",
        "/v1/swap",
    ]
    assert quote.quote_id == "quote-1"
    assert quote.minimum_to_amount == 38_707_500
    assert quote.transaction_to == ROUTER
    assert len(quote.preparation_transactions) == 1
    assert quote.preparation_transactions[0].transaction_to == FROM_TOKEN


def test_rejects_approval_for_a_different_swap_spender() -> None:
    handler, _ = _handler(approval_spender="0x2222222222222222222222222222222222222222")

    with pytest.raises(UniswapClientError, match="unexpected approval.spender"):
        _client(handler).get_swap_quote(
            from_token=FROM_TOKEN,
            to_token=TO_TOKEN,
            from_amount=AMOUNT,
            wallet_address=WALLET,
            slippage_percent=2.5,
        )


def test_surfaces_uniswap_quote_errors() -> None:
    handler, _ = _handler(quote_status=404)

    with pytest.raises(UniswapClientError, match="No quotes available"):
        _client(handler).get_swap_quote(
            from_token=FROM_TOKEN,
            to_token=TO_TOKEN,
            from_amount=AMOUNT,
            wallet_address=WALLET,
            slippage_percent=2.5,
        )
