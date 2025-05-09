import asyncio
import struct
from typing import Final, List

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
from trading.base import TokenInfo, Trader, TradeResult
from utils.logger import get_logger

logger = get_logger(__name__)

# Discriminator for the sell instruction
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 12502976635542562355)


class TokenSeller(Trader):
    """Handles selling tokens on pump.fun, with detailed diagnostics for ATA balance issues."""

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
        
        # Add grace period to prevent immediate selling after buying
        self.grace_period = 15  # seconds

    def _get_relevant_accounts(self, token_info: TokenInfo) -> List[Pubkey]:
        """Accounts to include when calculating priority fee."""
        return [token_info.bonding_curve, token_info.associated_bonding_curve]

    async def execute(self, token_info: TokenInfo, *args, **kwargs) -> TradeResult:
        """
        Execute sell operation, draining the user's ATA balance.
        Retries balance fetch and logs ATA and raw response for diagnostics.
        """
        try:
            # Derive user's ATA for this mint
            ata = self.wallet.get_associated_token_address(token_info.mint)
            logger.info(f"Derived ATA address: {ata}")

            # Retry fetching token balance with increasing delays
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
                        await asyncio.sleep(attempt * 2)  # Increasing delay between attempts
                except Exception as e:
                    logger.error(f"Error fetching balance on attempt {attempt}: {e}")
                    await asyncio.sleep(attempt * 2)  # Increasing delay between attempts

            if raw_balance == 0:
                error_msg = (
                    f"No tokens to sell (zero balance)" if balance_resp is not None
                    else f"ATA account not found or RPC error for {ata}"
                )
                logger.info(error_msg)
                return TradeResult(success=False, error_message=error_msg)

            token_balance = raw_balance / 10**TOKEN_DECIMALS
            logger.info(f"Token balance: {token_balance:.6f}")

            # Get current price per token
            curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve)
            price_per_token = curve_state.calculate_price()
            logger.info(f"Price per token: {price_per_token:.8f} SOL")

            # Compute minimum SOL out under slippage
            expected_sol = token_balance * price_per_token
            min_lamports = int((expected_sol * (1 - self.slippage)) * LAMPORTS_PER_SOL)
            logger.info(
                f"Selling {token_balance:.6f} tokens → expect {expected_sol:.8f} SOL "
                f"(min {min_lamports / LAMPORTS_PER_SOL:.8f} SOL)"
            )

            # Send transaction
            tx_sig = await self._send_sell_transaction(
                token_info,
                ata,
                raw_balance,
                min_lamports,
            )

            if await self.client.confirm_transaction(tx_sig):
                logger.info(f"Sell confirmed: {tx_sig}")
                return TradeResult(
                    success=True,
                    tx_signature=tx_sig,
                    amount=token_balance,
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
        """
        Construct & send the sell transaction using calculate_priority_fee.
        """
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

        # calculate priority fee just like buyer
        priority_fee = await self.priority_fee_manager.calculate_priority_fee(
            self._get_relevant_accounts(token_info)
        )

        # the SolanaClient will prepend any compute-unit instructions
        return await self.client.build_and_send_transaction(
            [sell_ix],
            self.wallet.keypair,
            skip_preflight=True,
            max_retries=self.max_retries,
            priority_fee=priority_fee,
        )
