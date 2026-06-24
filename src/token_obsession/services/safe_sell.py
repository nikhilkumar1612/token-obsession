"""Build and propose Safe transactions for full-position Base token sells."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

from eth_account import Account
from eth_utils import is_address, to_checksum_address
from hexbytes import HexBytes
from safe_eth.eth import EthereumClient, EthereumNetwork
from safe_eth.safe import Safe, SafeOperationEnum
from safe_eth.safe.api import TransactionServiceApi
from safe_eth.safe.multi_send import MultiSend, MultiSendOperation, MultiSendTx

from token_obsession.core.config import Settings
from token_obsession.core.models import (
    Position,
    SellProposalRecord,
    SellProposalStatus,
)
from token_obsession.services.uniswap import UniswapClient

logger = logging.getLogger(__name__)


class SafeSellProposalError(RuntimeError):
    """Raised when a sell cannot be safely built or proposed."""


@dataclass(frozen=True)
class SafeProposalChainStatus:
    """Current on-chain or Transaction Service state for a proposal."""

    status: SellProposalStatus
    execution_tx_hash: str | None = None
    failure_reason: str | None = None


class SafeSellProposalService:
    """Convert a tracked position into a Uniswap swap proposal for Safe Wallet."""

    _erc20_abi = [
        {
            "inputs": [],
            "name": "decimals",
            "outputs": [{"name": "", "type": "uint8"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [{"name": "account", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        },
    ]

    def __init__(
        self,
        settings: Settings,
        uniswap_client: UniswapClient | None = None,
    ) -> None:
        self._settings = settings
        self._uniswap_client = uniswap_client or UniswapClient(settings=settings)

    def propose_position_sell(self, position: Position) -> SellProposalRecord:
        """Build, validate, sign as a delegate, and post one Safe sell proposal."""

        logger.info(
            "SAFE SELL BUILD STARTED | token=%s | position_id=%s | tracked_quantity=%s | "
            "sell_percentage=%s",
            position.symbol,
            position.position_id,
            position.quantity,
            self._settings.sell_percentage,
        )
        logger.info("SAFE STEP 1/7 | Validating proposal configuration.")
        self._validate_configuration()
        logger.info("SAFE STEP 2/7 | Connecting to Base RPC and Safe Transaction Service.")
        ethereum_client, safe, transaction_api, safe_address = self._safe_dependencies()
        logger.info("SAFE CONNECTION READY | safe_address=%s | chain_id=%d", safe_address, 8453)
        delegate_private_key = self._required_secret(
            self._settings.safe_delegate_private_key,
            "TOKEN_OBSESSION_SAFE_DELEGATE_PRIVATE_KEY",
        )
        delegate_address = to_checksum_address(Account.from_key(delegate_private_key).address)
        logger.info(
            "SAFE STEP 3/7 | Verifying registered proposal delegate | delegate=%s",
            delegate_address,
        )
        self._require_registered_delegate(
            transaction_api=transaction_api,
            safe_address=safe_address,
            delegate_address=delegate_address,
        )
        logger.info("SAFE DELEGATE VERIFIED | delegate=%s", delegate_address)

        logger.info("SAFE STEP 4/7 | Reading token decimals and Safe token balance.")
        token_address = self._checksum_address(position.token_address, "token address")
        output_token_address = self._checksum_address(
            self._settings.base_usdc_address,
            "Base USDC address",
        )
        if token_address == output_token_address:
            raise SafeSellProposalError("The position is already Base USDC.")

        token_contract = ethereum_client.w3.eth.contract(
            address=token_address,
            abi=self._erc20_abi,
        )
        decimals = int(token_contract.functions.decimals().call())
        if decimals < 0 or decimals > 36:
            raise SafeSellProposalError(f"Unsupported token decimals: {decimals}")

        from_amount = self._position_amount_in_base_units(
            quantity=position.quantity,
            decimals=decimals,
            sell_percentage=self._settings.sell_percentage,
        )
        safe_balance = int(token_contract.functions.balanceOf(safe_address).call())
        logger.info(
            "SAFE TOKEN BALANCE CHECK | token=%s | decimals=%d | safe_balance_base_units=%d | "
            "required_sell_base_units=%d",
            position.symbol,
            decimals,
            safe_balance,
            from_amount,
        )
        # if safe_balance < from_amount:
        #     raise SafeSellProposalError(
        #         f"Safe balance is {safe_balance} base units but the tracked sell requires "
        #         f"{from_amount}.",
        #     )

        logger.info("SAFE STEP 5/7 | Requesting Uniswap approval and swap transactions.")
        quote = self._uniswap_client.get_swap_quote(
            from_token=token_address,
            to_token=output_token_address,
            from_amount=from_amount,
            wallet_address=safe_address,
            slippage_percent=self._settings.sell_slippage_percent,
        )
        logger.info(
            "SAFE UNISWAP QUOTE READY | quote_id=%s | estimated_output=%d | "
            "minimum_output=%d | preparation_transactions=%d",
            quote.quote_id,
            quote.estimated_to_amount,
            quote.minimum_to_amount,
            len(quote.preparation_transactions),
        )
        transaction_to = self._checksum_address(
            quote.transaction_to,
            "Uniswap transaction target",
        )
        if not ethereum_client.is_contract(transaction_to):
            raise SafeSellProposalError("Uniswap transaction target is not a contract.")
        transaction_data = HexBytes(quote.transaction_data)

        logger.info("SAFE STEP 6/7 | Building and estimating the Safe transaction batch.")
        inner_transactions: list[MultiSendTx] = []
        for preparation in quote.preparation_transactions:
            preparation_to = self._checksum_address(
                preparation.transaction_to,
                "Uniswap preparation target",
            )
            if preparation_to != token_address:
                raise SafeSellProposalError(
                    "Uniswap preparation transaction does not target the sold token."
                )
            inner_transactions.append(
                MultiSendTx(
                    MultiSendOperation.CALL,
                    preparation_to,
                    preparation.transaction_value,
                    HexBytes(preparation.transaction_data),
                )
            )

        inner_transactions.append(
            MultiSendTx(
                MultiSendOperation.CALL,
                transaction_to,
                quote.transaction_value,
                transaction_data,
            )
        )
        safe_nonce = self._next_safe_nonce(safe=safe, transaction_api=transaction_api)
        logger.info(
            "SAFE BATCH READY | inner_transaction_count=%d | safe_nonce=%d",
            len(inner_transactions),
            safe_nonce,
        )
        safe_tx = self._build_safe_transaction(
            ethereum_client=ethereum_client,
            safe=safe,
            inner_transactions=inner_transactions,
            safe_nonce=safe_nonce,
        )

        logger.info(
            "SAFE STEP 7/7 | Signing proposal with delegate and posting to Transaction Service."
        )
        safe_tx.sign(delegate_private_key)
        transaction_api.post_transaction(safe_tx)
        logger.warning(
            "SAFE PROPOSAL POSTED | token=%s | safe_tx_hash=%s | nonce=%d",
            position.symbol,
            safe_tx.safe_tx_hash.to_0x_hex(),
            safe_nonce,
        )
        timestamp = datetime.now(UTC)
        return SellProposalRecord(
            proposal_id=uuid4().hex,
            position_id=position.position_id,
            token_address=position.token_address,
            symbol=position.symbol,
            safe_address=safe_address,
            safe_tx_hash=safe_tx.safe_tx_hash.to_0x_hex(),
            safe_nonce=safe_nonce,
            quote_id=quote.quote_id,
            from_amount=from_amount,
            minimum_to_amount=quote.minimum_to_amount,
            output_token_address=output_token_address,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def get_proposal_status(
        self,
        proposal: SellProposalRecord,
    ) -> SafeProposalChainStatus:
        """Reconcile one stored proposal with Safe and the Base chain."""

        logger.info(
            "SAFE PROPOSAL RECONCILIATION STARTED | token=%s | safe_tx_hash=%s",
            proposal.symbol,
            proposal.safe_tx_hash,
        )
        self._validate_configuration()
        _, safe, transaction_api, safe_address = self._safe_dependencies()
        transactions = transaction_api.get_transactions(
            safe_address,
            safe_tx_hash=proposal.safe_tx_hash,
            limit=1,
        )
        if not transactions:
            raise SafeSellProposalError(
                f"Safe Transaction Service cannot find {proposal.safe_tx_hash}.",
            )

        transaction = transactions[0]
        if transaction.get("isExecuted"):
            execution_tx_hash = str(transaction.get("transactionHash") or "") or None
            if transaction.get("isSuccessful") is False:
                return SafeProposalChainStatus(
                    status=SellProposalStatus.FAILED,
                    execution_tx_hash=execution_tx_hash,
                    failure_reason="Safe transaction executed unsuccessfully.",
                )
            status = SafeProposalChainStatus(
                status=SellProposalStatus.EXECUTED,
                execution_tx_hash=execution_tx_hash,
            )
            logger.info(
                "SAFE PROPOSAL RECONCILIATION COMPLETED | status=%s | execution_tx_hash=%s",
                status.status.value,
                status.execution_tx_hash,
            )
            return status

        if safe.retrieve_nonce() > proposal.safe_nonce:
            return SafeProposalChainStatus(
                status=SellProposalStatus.SUPERSEDED,
                failure_reason="Safe nonce advanced without executing this proposal.",
            )
        logger.info("SAFE PROPOSAL RECONCILIATION COMPLETED | status=pending")
        return SafeProposalChainStatus(status=SellProposalStatus.PENDING)

    def _safe_dependencies(
        self,
    ) -> tuple[EthereumClient, Safe, TransactionServiceApi, str]:
        rpc_url = self._settings.base_rpc_url
        safe_address_raw = self._settings.safe_address
        if rpc_url is None or safe_address_raw is None:
            raise SafeSellProposalError("Safe RPC or address configuration is missing.")

        ethereum_client = EthereumClient(rpc_url)
        if ethereum_client.get_chain_id() != self._settings.base_chain_id:
            raise SafeSellProposalError("Configured RPC is not Base mainnet (chain ID 8453).")
        safe_address = self._checksum_address(safe_address_raw, "Safe address")
        if not ethereum_client.is_contract(safe_address):
            raise SafeSellProposalError("Configured Safe address is not a deployed contract.")

        safe = Safe(safe_address, ethereum_client)
        transaction_api = TransactionServiceApi(
            EthereumNetwork.BASE,
            ethereum_client=ethereum_client,
            api_key=self._required_secret(
                self._settings.safe_api_key,
                "TOKEN_OBSESSION_SAFE_API_KEY",
            ),
            request_timeout=self._settings.safe_request_timeout_seconds,
        )
        return ethereum_client, safe, transaction_api, safe_address

    def _validate_configuration(self) -> None:
        if not self._settings.sell_proposals_enabled:
            raise SafeSellProposalError(
                "Sell proposal submission is disabled. Set "
                "TOKEN_OBSESSION_SELL_PROPOSALS_ENABLED=true after Safe setup.",
            )

        missing: list[str] = []
        if not self._settings.base_rpc_url:
            missing.append("TOKEN_OBSESSION_BASE_RPC_URL")
        if not self._settings.safe_address:
            missing.append("TOKEN_OBSESSION_SAFE_ADDRESS")
        if self._settings.safe_api_key is None:
            missing.append("TOKEN_OBSESSION_SAFE_API_KEY")
        if self._settings.safe_delegate_private_key is None:
            missing.append("TOKEN_OBSESSION_SAFE_DELEGATE_PRIVATE_KEY")
        if self._settings.uniswap_api_key is None:
            missing.append("TOKEN_OBSESSION_UNISWAP_API_KEY")
        if missing:
            raise SafeSellProposalError(
                "Missing required sell proposal configuration: " + ", ".join(missing)
            )

    @staticmethod
    def _require_registered_delegate(
        transaction_api: TransactionServiceApi,
        safe_address: str,
        delegate_address: str,
    ) -> None:
        delegates = transaction_api.get_delegates(safe_address)
        if not any(
            str(delegate.get("delegate") or "").lower() == delegate_address.lower()
            for delegate in delegates
        ):
            raise SafeSellProposalError(
                f"Delegate {delegate_address} is not authorized for Safe {safe_address}.",
            )

    @staticmethod
    def _position_amount_in_base_units(
        quantity: float,
        decimals: int,
        sell_percentage: float,
    ) -> int:
        amount = (
            Decimal(str(quantity))
            * Decimal(10**decimals)
            * Decimal(str(sell_percentage))
            / Decimal(100)
        ).to_integral_value(rounding=ROUND_DOWN)
        amount_int = int(amount)
        if amount_int <= 0:
            raise SafeSellProposalError("Tracked position rounds down to zero base units.")
        return amount_int

    @staticmethod
    def _next_safe_nonce(
        safe: Safe,
        transaction_api: TransactionServiceApi,
    ) -> int:
        next_nonce = safe.retrieve_nonce()
        pending_transactions = transaction_api.get_transactions(
            safe.address,
            executed=False,
            ordering="-nonce",
            limit=1,
        )
        if pending_transactions:
            pending_nonce = int(pending_transactions[0]["nonce"])
            next_nonce = max(next_nonce, pending_nonce + 1)
        return next_nonce

    @staticmethod
    def _build_safe_transaction(
        ethereum_client: EthereumClient,
        safe: Safe,
        inner_transactions: list[MultiSendTx],
        safe_nonce: int,
    ):
        if len(inner_transactions) == 1:
            inner = inner_transactions[0]
            to = inner.to
            value = inner.value
            data = inner.data
            operation = SafeOperationEnum.CALL
        else:
            multi_send = MultiSend(ethereum_client=ethereum_client, call_only=True)
            if multi_send.address is None:
                raise SafeSellProposalError("Safe MultiSendCallOnly is unavailable on Base.")
            to = multi_send.address
            value = 0
            data = multi_send.build_tx_data(inner_transactions)
            operation = SafeOperationEnum.DELEGATE_CALL

        safe.estimate_tx_gas(to, value, data, operation)
        return safe.build_multisig_tx(
            to=to,
            value=value,
            data=data,
            operation=operation,
            safe_nonce=safe_nonce,
        )

    @staticmethod
    def _checksum_address(value: str, field: str) -> str:
        if not is_address(value):
            raise SafeSellProposalError(f"Invalid {field}: {value}")
        return to_checksum_address(value)

    @staticmethod
    def _required_secret(secret, variable_name: str) -> str:
        if secret is None:
            raise SafeSellProposalError(f"Missing required configuration: {variable_name}")
        return secret.get_secret_value()
