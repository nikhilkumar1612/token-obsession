"""Uniswap Trading API client for executable Base swap transactions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from eth_utils import is_address

from token_obsession.core.config import Settings
from token_obsession.core.models import PreparedSwapTransaction, SwapQuote


class UniswapClientError(RuntimeError):
    """Raised when Uniswap cannot return a safe executable swap."""


class UniswapClient:
    """Build a classic Uniswap Base swap without requiring Permit2 signatures."""

    _approve_selector = "095ea7b3"
    _router_version = "2.0"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    def get_swap_quote(
        self,
        from_token: str,
        to_token: str,
        from_amount: int,
        wallet_address: str,
        slippage_percent: float,
    ) -> SwapQuote:
        """Return validated approval and swap transactions for one exact-input sell."""

        api_key = self._settings.uniswap_api_key
        if api_key is None:
            raise UniswapClientError(
                "Missing required configuration: TOKEN_OBSESSION_UNISWAP_API_KEY"
            )

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": api_key.get_secret_value(),
            "x-permit2-disabled": "true",
            "x-universal-router-version": self._router_version,
        }
        with httpx.Client(
            base_url=self._settings.uniswap_base_url,
            headers=headers,
            timeout=self._settings.uniswap_timeout_seconds,
            transport=self._transport,
        ) as client:
            approval_payload = self._post(
                client,
                "/check_approval",
                {
                    "walletAddress": wallet_address,
                    "token": from_token,
                    "amount": str(from_amount),
                    "chainId": self._settings.base_chain_id,
                    "tokenOut": to_token,
                    "tokenOutChainId": self._settings.base_chain_id,
                },
                operation="approval check",
            )
            quote_response = self._post(
                client,
                "/quote",
                {
                    "type": "EXACT_INPUT",
                    "amount": str(from_amount),
                    "tokenInChainId": self._settings.base_chain_id,
                    "tokenOutChainId": self._settings.base_chain_id,
                    "tokenIn": from_token,
                    "tokenOut": to_token,
                    "swapper": wallet_address,
                    "recipient": wallet_address,
                    "slippageTolerance": slippage_percent,
                    "routingPreference": "BEST_PRICE",
                    "protocols": ["V2", "V3", "V4"],
                    "generatePermitAsTransaction": False,
                },
                operation="quote",
            )
            quote_payload = self._mapping(quote_response.get("quote"), "quote")
            deadline = datetime.now(UTC) + timedelta(
                minutes=self._settings.uniswap_swap_deadline_minutes
            )
            swap_response = self._post(
                client,
                "/swap",
                {
                    "quote": quote_payload,
                    "deadline": int(deadline.timestamp()),
                    # Approval is executed earlier in the same Safe MultiSend, so an
                    # isolated swap simulation can fail against current allowance state.
                    "simulateTransaction": False,
                    "safetyMode": "SAFE",
                },
                operation="swap build",
            )

        return self._normalize_quote(
            approval_payload=approval_payload,
            quote_response=quote_response,
            swap_response=swap_response,
            from_token=from_token,
            to_token=to_token,
            from_amount=from_amount,
            wallet_address=wallet_address,
        )

    def _normalize_quote(
        self,
        approval_payload: dict[str, Any],
        quote_response: dict[str, Any],
        swap_response: dict[str, Any],
        from_token: str,
        to_token: str,
        from_amount: int,
        wallet_address: str,
    ) -> SwapQuote:
        if str(quote_response.get("routing") or "") != "CLASSIC":
            raise UniswapClientError("Uniswap returned a non-classic route.")
        if quote_response.get("permitData") not in (None, {}):
            raise UniswapClientError("Uniswap unexpectedly requires a Permit2 signature.")
        if quote_response.get("permitTransaction") not in (None, {}):
            raise UniswapClientError("Uniswap unexpectedly returned a Permit2 transaction.")

        quote = self._mapping(quote_response.get("quote"), "quote")
        input_payload = self._mapping(quote.get("input"), "quote.input")
        output_payload = self._mapping(quote.get("output"), "quote.output")
        swap = self._mapping(swap_response.get("swap"), "swap")

        failure_reason = quote.get("txFailureReason") or quote_response.get("txFailureReason")
        if failure_reason:
            raise UniswapClientError(f"Uniswap quote simulation failed: {failure_reason}")

        self._require_int_equal(quote.get("chainId"), self._settings.base_chain_id, "chainId")
        self._require_int_equal(swap.get("chainId"), self._settings.base_chain_id, "swap.chainId")
        if str(quote.get("tradeType") or "") != "EXACT_INPUT":
            raise UniswapClientError("Uniswap returned an unexpected trade type.")
        self._require_address_equal(quote.get("swapper"), wallet_address, "quote.swapper")
        self._require_address_equal(input_payload.get("token"), from_token, "quote.input.token")
        self._require_address_equal(output_payload.get("token"), to_token, "quote.output.token")
        self._require_address_equal(
            output_payload.get("recipient"),
            wallet_address,
            "quote.output.recipient",
        )
        self._require_address_equal(swap.get("from"), wallet_address, "swap.from")
        self._require_int_equal(input_payload.get("amount"), from_amount, "quote.input.amount")

        estimated_to_amount = self._required_int(
            output_payload.get("amount"),
            "quote.output.amount",
        )
        minimum_to_amount = self._required_int(
            output_payload.get("minimumAmount"),
            "quote.output.minimumAmount",
        )
        if estimated_to_amount <= 0 or minimum_to_amount <= 0:
            raise UniswapClientError("Uniswap returned a non-positive output amount.")
        if minimum_to_amount > estimated_to_amount:
            raise UniswapClientError("Uniswap minimum output exceeds its estimated output.")

        transaction_to = self._required_address(swap.get("to"), "swap.to")
        transaction_data = self._required_data(swap.get("data"), "swap.data")
        transaction_value = self._required_int(swap.get("value", 0), "swap.value")
        if transaction_value != 0:
            raise UniswapClientError("Uniswap requested native value for an ERC-20 sell.")

        preparation_transactions = self._normalize_approvals(
            approval_payload=approval_payload,
            from_token=from_token,
            from_amount=from_amount,
            wallet_address=wallet_address,
            spender_address=transaction_to,
        )
        quote_id = str(quote.get("quoteId") or quote_response.get("requestId") or "")
        if not quote_id:
            raise UniswapClientError("Uniswap quote is missing its request identifier.")

        return SwapQuote(
            quote_id=quote_id,
            tool="uniswap-trading-api",
            preparation_transactions=preparation_transactions,
            transaction_to=transaction_to,
            transaction_data=transaction_data,
            transaction_value=transaction_value,
            from_amount=from_amount,
            estimated_to_amount=estimated_to_amount,
            minimum_to_amount=minimum_to_amount,
        )

    def _normalize_approvals(
        self,
        approval_payload: dict[str, Any],
        from_token: str,
        from_amount: int,
        wallet_address: str,
        spender_address: str,
    ) -> list[PreparedSwapTransaction]:
        transactions: list[PreparedSwapTransaction] = []
        for field, expected_amount in (("cancel", 0), ("approval", None)):
            raw_transaction = approval_payload.get(field)
            if raw_transaction is None:
                continue
            transaction = self._mapping(raw_transaction, field)
            self._require_int_equal(
                transaction.get("chainId"),
                self._settings.base_chain_id,
                f"{field}.chainId",
            )
            self._require_address_equal(transaction.get("from"), wallet_address, f"{field}.from")
            self._require_address_equal(transaction.get("to"), from_token, f"{field}.to")
            data = self._required_data(transaction.get("data"), f"{field}.data")
            spender, approval_amount = self._decode_approval(data, field)
            self._require_address_equal(spender, spender_address, f"{field}.spender")
            if expected_amount is not None and approval_amount != expected_amount:
                raise UniswapClientError(f"Uniswap {field} transaction has an invalid amount.")
            if field == "approval" and approval_amount < from_amount:
                raise UniswapClientError("Uniswap approval amount is below the sell amount.")
            value = self._required_int(transaction.get("value", 0), f"{field}.value")
            if value != 0:
                raise UniswapClientError(f"Uniswap {field} transaction has non-zero value.")
            transactions.append(
                PreparedSwapTransaction(
                    transaction_to=self._required_address(transaction.get("to"), f"{field}.to"),
                    transaction_data=data,
                    transaction_value=value,
                )
            )
        return transactions

    def _decode_approval(self, data: str, field: str) -> tuple[str, int]:
        encoded = data.removeprefix("0x")
        if len(encoded) != 136 or encoded[:8].lower() != self._approve_selector:
            raise UniswapClientError(f"Uniswap {field} is not an ERC-20 approve transaction.")
        arguments = encoded[8:]
        spender = "0x" + arguments[:64][-40:]
        amount = int(arguments[64:128], 16)
        return spender, amount

    @staticmethod
    def _post(
        client: httpx.Client,
        path: str,
        payload: dict[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        try:
            response = client.post(path, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise UniswapClientError(
                UniswapClient._error_message(exc.response, operation)
            ) from exc
        except httpx.HTTPError as exc:
            raise UniswapClientError(f"Uniswap {operation} request failed: {exc}") from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise UniswapClientError(
                f"Uniswap {operation} returned invalid JSON."
            ) from exc
        if not isinstance(result, dict):
            raise UniswapClientError(f"Uniswap {operation} returned an unexpected response.")
        return result

    @staticmethod
    def _mapping(value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise UniswapClientError(f"Uniswap response is missing {field}.")
        return value

    @staticmethod
    def _required_address(value: Any, field: str) -> str:
        address = str(value or "")
        if not is_address(address):
            raise UniswapClientError(f"Uniswap returned an invalid {field}.")
        return address

    @staticmethod
    def _require_address_equal(actual: Any, expected: str, field: str) -> None:
        address = UniswapClient._required_address(actual, field)
        if address.lower() != expected.lower():
            raise UniswapClientError(f"Uniswap returned an unexpected {field}.")

    @staticmethod
    def _required_data(value: Any, field: str) -> str:
        data = str(value or "")
        if not data.startswith("0x") or len(data) <= 2:
            raise UniswapClientError(f"Uniswap returned invalid {field}.")
        try:
            bytes.fromhex(data[2:])
        except ValueError as exc:
            raise UniswapClientError(f"Uniswap returned invalid {field}.") from exc
        return data

    @staticmethod
    def _required_int(value: Any, field: str) -> int:
        try:
            if isinstance(value, str) and value.startswith("0x"):
                return int(value, 16)
            return int(value)
        except (TypeError, ValueError) as exc:
            raise UniswapClientError(f"Uniswap returned an invalid {field}.") from exc

    @staticmethod
    def _require_int_equal(actual: Any, expected: int, field: str) -> None:
        if UniswapClient._required_int(actual, field) != expected:
            raise UniswapClientError(f"Uniswap returned an unexpected {field}.")

    @staticmethod
    def _error_message(response: httpx.Response, operation: str) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"Uniswap {operation} failed with HTTP {response.status_code}."
        if not isinstance(payload, dict):
            return f"Uniswap {operation} failed with HTTP {response.status_code}."
        message = str(payload.get("message") or payload.get("error") or "Unknown API error")
        return f"Uniswap {operation} failed with HTTP {response.status_code}: {message}"
