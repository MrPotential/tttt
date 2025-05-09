# buyer.py
"""
Buy operations for pump.fun tokens.
"""

import struct
from typing import Final

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import create_idempotent_associated_token_account

from core.client import SolanaClient
from core.curve import BondingCurveManager
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    LAMPORTS_PER_SOL,
    TOKEN_DECIMALS,
    PumpAddresses,
    SystemAddresses,
)
from core.wallet import Wallet
from trading.base import TokenInfo, Trader, TradeResult
from utils.logger import get_logger

logger = get_logger(__name__)

# Discriminator for the buy instruction
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 16927863322537952870)


class TokenBuyer(Trader):
    """Handles buying tokens on pump.fun."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        curve_manager: BondingCurveManager,
        priority_fee_manager: PriorityFeeManager,
        amount: float,
        slippage: float = 0.01,
        max_retries: int = 5,
        extreme_fast_token_amount: int = 0,
        extreme_fast_mode: bool = False,
    ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.priority_fee_manager = priority_fee_manager
        self.amount = amount
        self.slippage = slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount

    async def execute(self, token_info: TokenInfo, *args, **kwargs) -> TradeResult:
        """Execute buy operation."""
        try:
            amount_lamports = int(self.amount * LAMPORTS_PER_SOL)

            if self.extreme_fast_mode:
                token_amount = self.extreme_fast_token_amount
                token_price_sol = self.amount / token_amount
            else:
                curve_state = await self.curve_manager.get_curve_state(
                    token_info.bonding_curve
                )
                token_price_sol = curve_state.calculate_price()
                token_amount = self.amount / token_price_sol

            max_amount_lamports = int(amount_lamports * (1 + self.slippage))

            ata = self.wallet.get_associated_token_address(token_info.mint)

            tx = await self._send_buy_transaction(
                token_info, ata, token_amount, max_amount_lamports
            )

            logger.info(
                f"Buying {token_amount:.6f} tokens at {token_price_sol:.8f} SOL per token"
            )
            logger.info(
                f"Total cost: {self.amount:.6f} SOL (max: {max_amount_lamports / LAMPORTS_PER_SOL:.6f} SOL)"
            )

            confirmed = await self.client.confirm_transaction(tx)
            if confirmed:
                logger.info(f"Buy confirmed: {tx}")
                return TradeResult(
                    success=True, tx_signature=tx, amount=token_amount, price=token_price_sol
                )
            else:
                return TradeResult(success=False, error_message=f"Confirmation failed: {tx}")

        except Exception as e:
            logger.error(f"Buy operation failed: {e}")
            return TradeResult(success=False, error_message=str(e))

    async def _send_buy_transaction(
        self,
        token_info: TokenInfo,
        associated_token_account: Pubkey,
        token_amount: float,
        max_amount_lamports: int,
    ) -> str:
        """Build & send the buy tx, including an idempotent ATA ix."""
        accounts = [
            AccountMeta(pubkey=PumpAddresses.GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PumpAddresses.FEE, is_signer=False, is_writable=True),
            AccountMeta(pubkey=token_info.mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=token_info.bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=token_info.associated_bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
            AccountMeta(pubkey=SystemAddresses.PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SystemAddresses.TOKEN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SystemAddresses.RENT, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PumpAddresses.PROGRAM, is_signer=False, is_writable=False),
        ]

        # 1️⃣ Idempotent ATA creation (no 'program_id' argument)
        ata_ix = create_idempotent_associated_token_account(
            self.wallet.pubkey, self.wallet.pubkey, token_info.mint
        )

        # 2️⃣ Buy instruction data
        raw_amount = int(token_amount * 10**TOKEN_DECIMALS)
        data = (
            EXPECTED_DISCRIMINATOR
            + struct.pack("<Q", raw_amount)
            + struct.pack("<Q", max_amount_lamports)
        )
        buy_ix = Instruction(PumpAddresses.PROGRAM, data, accounts)

        return await self.client.build_and_send_transaction(
            [ata_ix, buy_ix],
            self.wallet.keypair,
            skip_preflight=True,
            max_retries=self.max_retries,
            priority_fee=await self.priority_fee_manager.calculate_priority_fee(
                self._get_relevant_accounts(token_info)
            ),
        )
