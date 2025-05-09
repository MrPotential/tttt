from dataclasses import dataclass, field
from time import monotonic
from typing import List, Optional, Dict
import asyncio
from solders.pubkey import Pubkey

from trading.base import TokenInfo, TradeResult
from utils.logger import get_logger
from colorama import Fore, Style

logger = get_logger(__name__)

@dataclass
class Position:
    """Tracks an open trading position with accurate ROI and robust exit logic."""

    token_info: TokenInfo
    entry_price: float
    amount: float
    buy_time: float = field(default_factory=monotonic)
    tx_signature: Optional[str] = None

    # --- New fields for accurate ROI tracking ---
    original_amount: float = field(init=False)
    original_cost: float = field(init=False)
    realized_profit: float = field(default=0.0)

    peak_price: float = 0.0
    unrealized_pnl_pct: float = 0.0

    take_profit_tiers: List[float] = field(default_factory=list)
    stop_loss_pct: float = 0.10
    trailing_stop_pct: float = 0.10

    current_tier: int = 0
    sell_portions: List[float] = field(default_factory=lambda: [0.5, 0.5])
    amount_remaining: float = 0.0

    last_valid_price: float = 0.0
    price_check_count: int = 0
    min_valid_price_threshold: float = 0.01

    min_hold_time: float = 10.0  # minimum hold time before exit triggers

    def __post_init__(self):
        # Initialize ROI tracking fields
        self.original_amount = self.amount
        self.original_cost = self.entry_price * self.amount

        # Initialize price and stop logic
        self.peak_price = self.entry_price
        self.amount_remaining = self.amount
        self.last_valid_price = self.entry_price
        self.stop_price = self.entry_price * (1 - self.stop_loss_pct)

    @property
    def token_key(self) -> str:
        return str(self.token_info.mint)

    @property
    def symbol(self) -> str:
        return self.token_info.symbol

    @property
    def roi_pct(self) -> float:
        """
        Calculate total ROI: (remaining value + realized profit) / original cost - 1
        """
        total_value = self.amount_remaining * self.last_valid_price + self.realized_profit
        return (total_value / self.original_cost - 1) * 100

    def update(self, current_price: float) -> None:
        min_valid_price = self.entry_price * self.min_valid_price_threshold

        if current_price <= 0 or current_price < min_valid_price:
            self.price_check_count += 1
            if self.price_check_count % 10 == 0:
                logger.warning(f"Invalid price {current_price} for {self.symbol} (threshold: {min_valid_price})")
            return

        # Reset invalid count and update price
        self.price_check_count = 0
        self.last_valid_price = current_price

        # Update unrealized PnL percentage
        self.unrealized_pnl_pct = ((current_price / self.entry_price) - 1) * 100

        # Update trailing-stop thresholds on new peaks
        if current_price > self.peak_price:
            self.peak_price = current_price
            self.stop_price = current_price * (1 - self.trailing_stop_pct)

    def get_exit_info(self, current_price: float) -> Optional[Dict]:
        self.update(current_price)

        # Enforce minimum hold time
        if monotonic() - self.buy_time < self.min_hold_time:
            return None

        price_to_use = self.last_valid_price
        if price_to_use <= 0:
            return None

        # Check stop-loss or trailing stop
        if price_to_use <= self.stop_price:
            reason = "trailing_stop" if self.peak_price > self.entry_price else "stop_loss"
            logger.warning(
                f"{Fore.RED}⚠️ {reason.upper()} triggered for {self.symbol}: {price_to_use:.8f} vs stop {self.stop_price:.8f} | ROI: {self.roi_pct:.2f}%{Style.RESET_ALL}"
            )
            return {
                "reason": reason,
                "price": price_to_use,
                "portion": 1.0,
                "message": f"{Fore.RED}⚠️ {reason.upper()} triggered for {self.symbol} at {price_to_use:.8f}{Style.RESET_ALL}"
            }

        # Check take-profit tiers (partial exits)
        if self.current_tier < len(self.take_profit_tiers):
            target_price = self.entry_price * self.take_profit_tiers[self.current_tier]
            if price_to_use >= target_price:
                portion = self.sell_portions[self.current_tier]
                logger.info(
                    f"{Fore.GREEN}🚀 TAKE PROFIT TIER {self.current_tier + 1} for {self.symbol} @ {price_to_use:.8f} (+{self.roi_pct:.2f}%) | Entry: {self.entry_price}{Style.RESET_ALL}"
                )
                return {
                    "reason": f"take_profit_tier_{self.current_tier+1}",
                    "price": price_to_use,
                    "portion": portion,
                    "tier": self.current_tier,
                    "message": f"{Fore.GREEN}🚀 TAKE PROFIT TIER {self.current_tier+1} triggered for {self.symbol} at {price_to_use:.8f} (+{self.roi_pct:.2f}%){Style.RESET_ALL}"
                }

        return None

    def mark_tier_taken(self) -> None:
        portion = self.sell_portions[self.current_tier]
        self.amount_remaining *= (1 - portion)
        self.current_tier += 1


class PositionManager:
    def __init__(
        self,
        take_profit_tiers: List[float] = [1.2, 1.5, 2.0],
        sell_portions: List[float] = [0.5, 0.5, 1.0],
        stop_loss_pct: float = 0.10,
        trailing_stop_pct: float = 0.10,
        max_positions: int = 3
    ):
        self.positions: Dict[str, Position] = {}
        self.take_profit_tiers = take_profit_tiers
        self.sell_portions = sell_portions
        self.stop_loss_pct = stop_loss_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.max_positions = max_positions

        # Ensure lists align
        if len(sell_portions) != len(take_profit_tiers):
            logger.warning("Mismatch between take_profit_tiers and sell_portions. Adjusting to defaults.")
            self.sell_portions = [1 / len(take_profit_tiers)] * (len(take_profit_tiers) - 1) + [1.0]

        logger.info(
            f"PositionManager initialized. Max positions: {max_positions}, TP: {take_profit_tiers}, SL: {stop_loss_pct}, TS: {trailing_stop_pct}"
        )

    def add_position(self, token_info: TokenInfo, buy_result: TradeResult) -> Position:
        token_key = str(token_info.mint)
        position = Position(
            token_info=token_info,
            entry_price=buy_result.price,
            amount=buy_result.amount,
            tx_signature=buy_result.tx_signature,
            take_profit_tiers=self.take_profit_tiers,
            sell_portions=self.sell_portions,
            stop_loss_pct=self.stop_loss_pct,
            trailing_stop_pct=self.trailing_stop_pct
        )
        self.positions[token_key] = position
        logger.info(f"Position added: {token_info.symbol} at {buy_result.price:.8f} SOL")
        return position

    def get_position(self, token_key: str) -> Optional[Position]:
        return self.positions.get(token_key)

    def remove_position(self, token_key: str) -> None:
        if token_key in self.positions:
            logger.info(f"Position closed: {token_key}")
            del self.positions[token_key]

    def has_capacity(self) -> bool:
        return len(self.positions) < self.max_positions

    def get_position_count(self) -> int:
        return len(self.positions)
