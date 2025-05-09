import asyncio
import struct
from typing import Final, List, Optional

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

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
from trading.base import TokenInfo, TradeResult
from utils.logger import get_logger

logger = get_logger(__name__)

# Discriminator for the sell instruction
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 12502976635542562355)


def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID
    seeds = [
        bytes(owner),
        bytes(TOKEN_PROGRAM_ID),
        bytes(mint),
    ]
    return Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)[0]


class TokenSeller:
    """Handles selling tokens on pump.fun with support for partial sells and diagnostics."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        curve_manager: BondingCurveManager,
        priority_fee_manager: PriorityFeeManager,
        slippage: float = 0.25,
        max_retries: int = 5,
    ):
        self.client = client
        self.wallet = wallet
        self.curve_manager = curve_manager
        self.priority_fee_manager = priority_fee_manager
        self.slippage = slippage
        self.max_retries = max_retries
        self.grace_period = 15  # seconds

    def _get_relevant_accounts(self, token_info: TokenInfo) -> List[Pubkey]:
        return [token_info.bonding_curve, token_info.associated_bonding_curve]

    async def execute(
        self,
        token_info: TokenInfo,
        amount: Optional[float] = None,  # optional partial sell amount (in tokens)
        *args,
        **kwargs
    ) -> TradeResult:
        try:
            ata = get_associated_token_address(self.wallet.pubkey, token_info.mint)
            logger.info(f"Derived ATA address: {ata}")

            balance_resp = None
            raw_balance = 0
            for attempt in range(1, self.max_retries + 1):
                try:
                    balance_resp = await self.client.get_token_account_balance(ata)
                    logger.info(f"Balance response (attempt {attempt}): {balance_resp}")
                    raw_balance = int(balance_resp or 0)
                    if raw_balance > 0:
                        logger.info(f"Found non-zero balance: {raw_balance} raw units")
                        break
                    else:
                        logger.debug("Balance is zero, retrying...")
                        await asyncio.sleep(attempt * 2)
                except Exception as e:
                    if "could not find account" in str(e):
                        logger.warning(f"ATA not found yet. Retrying in {attempt} sec...")
                    else:
                        logger.error(f"Error fetching balance on attempt {attempt}: {e}")
                    await asyncio.sleep(attempt * 2)

            if raw_balance == 0:
                error_msg = (
                    f"No tokens to sell (zero balance)" if balance_resp is not None
                    else f"ATA account not found or RPC error for {ata}"
                )
                logger.info(error_msg)
                return TradeResult(success=False, error_message=error_msg)

            decimals = TOKEN_DECIMALS
            token_balance = raw_balance / (10 ** decimals)
            logger.info(f"Token balance: {token_balance:.6f}")

            # Determine amount to sell: full vs partial
            sell_amount = token_balance if amount is None else min(token_balance, amount)
            raw_to_sell = int(sell_amount * (10 ** decimals))

            curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve)
            price_per_token = curve_state.calculate_price()
            logger.info(f"Price per token: {price_per_token:.8f} SOL")

            expected_sol = sell_amount * price_per_token
            min_lamports = int((expected_sol * (1 - self.slippage)) * LAMPORTS_PER_SOL)
            logger.info(
                f"Selling {sell_amount:.6f} tokens → expect {expected_sol:.8f} SOL "
                f"(min {min_lamports / LAMPORTS_PER_SOL:.8f} SOL)"
            )

            tx_sig = await self._send_sell_transaction(
                token_info,
                ata,
                raw_to_sell,
                min_lamports,
            )

            if await self.client.confirm_transaction(tx_sig):
                logger.info(f"Sell confirmed: {tx_sig}")
                return TradeResult(
                    success=True,
                    tx_signature=tx_sig,
                    amount=sell_amount,
                    price=price_per_token,
                )
            else:
                error_msg = f"Confirmation failed: {tx_sig}"
                logger.error(error_msg)
                return TradeResult(success=False, error_message=error_msg)

        except Exception as e:
            logger.error(f"Sell operation failed: {e}")
            return TradeResult(success=False, error_message=str(e))

    async def _send_sell_transaction(
        self,
        token_info: TokenInfo,
        ata: Pubkey,
        token_amount_raw: int,
        min_sol_lamports: int,
    ) -> str:
        accounts = [
            AccountMeta(pubkey=PumpAddresses.GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PumpAddresses.FEE, is_signer=False, is_writable=True),
            AccountMeta(pubkey=token_info.mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=token_info.bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=token_info.associated_bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
            AccountMeta(pubkey=SystemAddresses.PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SystemAddresses.ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SystemAddresses.TOKEN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PumpAddresses.EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PumpAddresses.PROGRAM, is_signer=False, is_writable=False),
        ]

        data = (
            EXPECTED_DISCRIMINATOR
            + struct.pack("<Q", token_amount_raw)
            + struct.pack("<Q", min_sol_lamports)
        )
        sell_ix = Instruction(PumpAddresses.PROGRAM, data, accounts)

        priority_fee = await self.priority_fee_manager.calculate_priority_fee(
            self._get_relevant_accounts(token_info)
        )

        return await self.client.build_and_send_transaction(
            [sell_ix],
            self.wallet.keypair,
            skip_preflight=True,
            max_retries=self.max_retries,
            priority_fee=priority_fee,
        )
