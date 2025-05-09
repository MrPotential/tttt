# === Updated manager.py ===
from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee.dynamic_fee import DynamicPriorityFee
from core.priority_fee.fixed_fee import FixedPriorityFee
from utils.logger import get_logger

logger = get_logger(__name__)

class PriorityFeeManager:
    """Manager for priority fee calculation and validation."""

    def __init__(
        self,
        client: SolanaClient,
        enable_dynamic_fee: bool,
        enable_fixed_fee: bool,
        fixed_fee: int,
        extra_fee: float,
        hard_cap: int,
    ):
        self.client = client
        self.enable_dynamic_fee = enable_dynamic_fee
        self.enable_fixed_fee = enable_fixed_fee
        self.fixed_fee = fixed_fee
        self.extra_fee = extra_fee
        self.hard_cap = hard_cap

        self.dynamic_fee_plugin = DynamicPriorityFee(client)
        self.fixed_fee_plugin = FixedPriorityFee(fixed_fee)

    async def calculate_priority_fee(
        self, accounts: list[Pubkey] | None = None
    ) -> int | None:
        base_fee = await self._get_base_fee(accounts)
        if base_fee is None:
            return None

        final_fee = int(base_fee * (1 + self.extra_fee))

        if final_fee > self.hard_cap:
            logger.warning(
                f"Calculated priority fee {final_fee} exceeds hard cap {self.hard_cap}. Applying hard cap."
            )
            final_fee = self.hard_cap

        return final_fee

    async def _get_base_fee(self, accounts: list[Pubkey] | None = None) -> int | None:
        if self.enable_dynamic_fee:
            dynamic_fee = await self.dynamic_fee_plugin.get_priority_fee(accounts)
            if dynamic_fee is not None:
                return dynamic_fee

        if self.enable_fixed_fee:
            return await self.fixed_fee_plugin.get_priority_fee()

        return None

    async def get_priority_fee(self) -> int:
        """Public method to get the priority fee depending on config."""
        if self.enable_fixed_fee:
            return await self.fixed_fee_plugin.get_priority_fee()
        if self.enable_dynamic_fee:
            dynamic_fee = await self.dynamic_fee_plugin.get_priority_fee()
            return dynamic_fee or 0
        return 0
