"""
Main trading coordinator for pump.fun tokens.
Volume-based trading strategy with clean terminal output.
"""

import asyncio
import json
import os
import logging
from datetime import datetime
from time import monotonic
from typing import Dict, Set, Callable, List, Optional, Tuple;
from core.pubkeys import PumpAddresses


import uvloop
import colorama
from colorama import Fore, Style
from solders.pubkey import Pubkey

from cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)
from core.client import SolanaClient
from core.curve import BondingCurveManager
from core.priority_fee.manager import PriorityFeeManager
from core.wallet import Wallet
from monitoring.block_listener import BlockListener
from monitoring.geyser_listener import GeyserListener
from monitoring.logs_listener import LogsListener
from trading.base import TokenInfo, TradeResult
from trading.buyer import TokenBuyer
from trading.seller import TokenSeller
from trading.position_manager import PositionManager, Position
from analytics.analytics import TradingAnalytics
from utils.logger import get_logger

# Configure detailed logging to file
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("pump_bot_debug.log"),
        logging.StreamHandler()
    ]
)

# Initialize colorama for cross-platform colored terminal output
colorama.init()

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

# Configure logging - HIDE HTTP REQUEST LOGS
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = get_logger(__name__)

# Silence the specific curve state errors to reduce spam
curve_logger = logging.getLogger("core.curve")
curve_logger.addFilter(lambda record: "Failed to get curve state:" not in record.getMessage())

# Constants for position management
TAKE_PROFIT_PCT = 0.20  # 20% take profit
STOP_LOSS_PCT = 0.10    # 10% stop loss
TRAILING_STOP_PCT = 0.10  # 10% trailing stop
MAX_OPEN_POSITIONS = 3  # Maximum concurrent positions

# Improved PumpPortal listener that subscribes to token trade data
class PumpPortalListener:
    """Listens to PumpPortal WebSocket API for token creation and trade events."""

    def __init__(self, endpoint="wss://pumpportal.fun/api/data"):
        """Initialize PumpPortal listener.
        
        Args:
            endpoint: PumpPortal WebSocket API endpoint
        """
        self.endpoint = endpoint
        self.websocket = None
        self.callback = None
        self.match_string = None
        self.bro_address = None
        self.subscribed_tokens = set()
        self.token_volumes = {}
        self.running = False  # Start as not running
        
        # Diagnostic state
        self.messages_received = 0
        self.token_creation_events = 0
        self.trade_events = 0
        
        # Use simple logger for cleaner output
        self.logger = get_logger(__name__)
    
    async def listen_for_tokens(
        self,
        callback: Callable[[TokenInfo], None],
        match_string: str = None,
        bro_address: str = None,
    ) -> None:
        """Listen for new token events."""
        print(f"{Fore.YELLOW}🔌 Starting PumpPortal listener...{Style.RESET_ALL}")
        self.callback = callback
        self.match_string = match_string
        self.bro_address = bro_address
        self.running = True  # Set to running when method is called
        
        import websockets
        retry_count = 0
        max_retries = 10
        
        while retry_count < max_retries and self.running:
            try:
                print(f"{Fore.CYAN}⚡ Connecting to PumpPortal WebSocket...{Style.RESET_ALL}")
                
                async with websockets.connect(self.endpoint) as self.websocket:
                    print(f"{Fore.GREEN}✓ Connected to PumpPortal WebSocket{Style.RESET_ALL}")
                    
                    # Subscribe to token creation events
                    await self._subscribe_to_new_tokens()
                    print(f"{Fore.YELLOW}Listening for token events...{Style.RESET_ALL}")
                    
                    # Listen for incoming messages
                    last_message_time = monotonic()
                    
                    while self.running:
                        try:
                            # Set timeout to detect connection issues
                            message = await asyncio.wait_for(self.websocket.recv(), timeout=30)
                            last_message_time = monotonic()
                            self.messages_received += 1
                            
                            # Process message
                            data = json.loads(message)
                            
                            # Print diagnostic stats less frequently to reduce spam
                            if self.messages_received % 100 == 0:
                                print(f"{Fore.CYAN}📊 Stats: {self.messages_received} messages | {self.token_creation_events} tokens | {self.trade_events} trades{Style.RESET_ALL}")
                            
                            # Identify message type based on txType first
                            tx_type = data.get("txType")
                            
                            if tx_type == "create":
                                # Token creation event
                                self.token_creation_events += 1
                                token_mint = data.get("mint")
                                name = data.get("name", "")
                                symbol = data.get("symbol", "")
                                
                                print(f"{Fore.GREEN}💎 New token: {symbol} ({token_mint[:8]}...){Style.RESET_ALL}")
                                await self._handle_new_token(data)
                                
                            elif tx_type in ["buy", "sell"]:
                                # Handle trade message silently - only update counters to reduce spam
                                self.trade_events += 1
                                await self._handle_token_trade(data)
                                
                            elif "message" in data:
                                # Service message
                                self.logger.debug(f"Service message: {data.get('message', 'Unknown')}")
                                
                            elif "signature" in data and "mint" in data and self.messages_received <= 5:
                                # Just log a few signature messages for debugging
                                # Skip most of these to reduce clutter
                                pass
                                
                        except asyncio.TimeoutError:
                            current_time = monotonic()
                            idle_time = current_time - last_message_time
                            print(f"{Fore.RED}⚠️ No messages for {idle_time:.1f} seconds, checking connection...{Style.RESET_ALL}")
                            
                            try:
                                pong_waiter = await self.websocket.ping()
                                await asyncio.wait_for(pong_waiter, timeout=5)
                                print(f"{Fore.GREEN}✓ WebSocket ping succeeded{Style.RESET_ALL}")
                            except:
                                print(f"{Fore.RED}✗ WebSocket ping failed, reconnecting...{Style.RESET_ALL}")
                                # Break inner loop to reconnect
                                break
                                
                        except json.JSONDecodeError:
                            print(f"{Fore.RED}✗ Invalid JSON received{Style.RESET_ALL}")
                            
                        except Exception as e:
                            print(f"{Fore.RED}✗ Error processing message: {e}{Style.RESET_ALL}")
                
                # If we're here, the connection was lost
                retry_count += 1
                print(f"{Fore.YELLOW}Connection lost. Retry {retry_count}/{max_retries}...{Style.RESET_ALL}")
                await asyncio.sleep(5)  # Wait before reconnecting
                
            except Exception as e:
                retry_count += 1
                print(f"{Fore.RED}✗ WebSocket error: {e!s}. Retry {retry_count}/{max_retries}...{Style.RESET_ALL}")
                await asyncio.sleep(5)  # Wait before reconnecting
        
        if not self.running:
            print(f"{Fore.YELLOW}Listener stopped by request{Style.RESET_ALL}")
        else:
            print(f"{Fore.RED}✗ Maximum retries reached.{Style.RESET_ALL}")
    
    async def _subscribe_to_new_tokens(self) -> None:
        """Subscribe to new token creation events."""
        if not self.websocket:
            return
            
        payload = {"method": "subscribeNewToken"}
        await self.websocket.send(json.dumps(payload))
        self.logger.debug(f"✓ Subscribed to new token events")
    
    async def _subscribe_to_token_trades(self, token_mint: str) -> None:
        """Subscribe to trade events for a specific token.
        
        Args:
            token_mint: Token mint address
        """
        if not self.websocket or token_mint in self.subscribed_tokens:
            return
            
        payload = {
            "method": "subscribeTokenTrade",
            "keys": [token_mint]
        }
        await self.websocket.send(json.dumps(payload))
        self.subscribed_tokens.add(token_mint)
        self.logger.debug(f"📊 Subscribed to trades for: {token_mint[:8]}...")
    
    async def _unsubscribe_from_token_trades(self, token_mint: str) -> None:
        """Unsubscribe from trade events for a specific token.
        
        Args:
            token_mint: Token mint address
        """
        if not self.websocket or token_mint not in self.subscribed_tokens:
            return
            
        payload = {
            "method": "unsubscribeTokenTrade",
            "keys": [token_mint]
        }
        await self.websocket.send(json.dumps(payload))
        self.subscribed_tokens.discard(token_mint)
        self.logger.debug(f"🛑 Unsubscribed from trades for: {token_mint[:8]}...")
    
    async def _handle_new_token(self, data: dict) -> None:
        """Handle new token detection event.
        
        Args:
            data: Token data from PumpPortal
        """
        token_mint = data.get("mint")
        name = data.get("name", "")
        symbol = data.get("symbol", "")
        
        # Apply filters if specified
        if self.match_string and self.match_string.lower() not in (name + symbol).lower():
            return
            
        if self.bro_address and data.get("traderPublicKey", "") != self.bro_address:
            return
        
        try:
            # Create mint pubkey
            mint_pubkey = Pubkey.from_string(token_mint)
            
            # IMPORTANT CHANGE: Get bonding curve key directly from message
            bonding_curve_key = data.get("bondingCurveKey")
            if not bonding_curve_key:
                print(f"{Fore.RED}✗ No bondingCurveKey found in message for {symbol}{Style.RESET_ALL}")
                return
                
            bonding_curve = Pubkey.from_string(bonding_curve_key)
            
            # FIXED: Derive the associated token account for the bonding curve
            # instead of setting it to None
            associated_bonding_curve = PumpAddresses.get_associated_bonding_curve_address(
                bonding_curve,
                mint_pubkey
            )
            
            self.logger.debug(f"✓ Using bonding curve: {bonding_curve_key} for {symbol}")
            self.logger.debug(f"✓ Derived associated account: {associated_bonding_curve}")
            
            # Default URIs for pump.fun tokens
            uri = data.get("uri", f"https://pump.fun/token/{token_mint}")
            
            # Construct token information with all required fields
            token_info = TokenInfo(
                mint=mint_pubkey,
                name=name,
                symbol=symbol,
                uri=uri,
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                user=None,  # User will be set later when needed
            )
            
            # Subscribe to token trades
            await self._subscribe_to_token_trades(token_mint)
            
            # Initialize volume tracking
            self.token_volumes[token_mint] = 0.0
            
            # Invoke callback
            if self.callback:
                await self.callback(token_info)
                
        except Exception as e:
            print(f"{Fore.RED}✗ Error creating TokenInfo: {e}{Style.RESET_ALL}")
    
    async def _handle_token_trade(self, data: dict) -> None:
        """Handle token trade event.
        
        Args:
            data: Trade data from PumpPortal
        """
        try:
            # Check if this is a trade message (buy or sell)
            tx_type = data.get("txType")
            if tx_type not in ["buy", "sell"]:
                return
                
            token_mint = data.get("mint")
            
            if not token_mint or token_mint not in self.token_volumes:
                return
                
            # Get trade amount in SOL
            sol_amount = float(data.get("solAmount", 0))
            
            # Add to token's volume
            self.token_volumes[token_mint] += sol_amount
            
            # NOTE: We're no longer printing each trade to reduce terminal spam
            # Only significant volume changes will be shown in the monitoring loop
            
        except Exception as e:
            print(f"{Fore.RED}✗ Error processing trade data: {e!s}{Style.RESET_ALL}")
    
    def get_token_volume(self, token_mint: str) -> float:
        """Get current trading volume for a token in SOL.
        
        Args:
            token_mint: Token mint address
            
        Returns:
            Volume in SOL
        """
        return self.token_volumes.get(token_mint, 0.0)
    
    def stop(self) -> None:
        """Stop the listener."""
        print(f"{Fore.YELLOW}Stopping PumpPortal listener...{Style.RESET_ALL}")
        self.running = False


class TokenMonitor:
    """Monitors token metrics like volume, price, and market conditions."""
    
    def __init__(self, token_info: TokenInfo, monitor_duration: int = 30):
        self.token_info = token_info
        self.monitor_start_time = monotonic()
        self.monitor_duration = monitor_duration
        self.volume_sol = 0.0
        self.price_sol = 0.0
        self.last_check_time = 0
        self.monitoring_active = True
        self.volume_target_reached = False
        
        # Enhanced tracking for analysis
        self.price_history = []  # List of (timestamp, price) tuples
        self.volume_history = []  # List of (timestamp, volume_delta) tuples
        
    @property
    def elapsed_time(self) -> float:
        """Get elapsed monitoring time in seconds."""
        return monotonic() - self.monitor_start_time
        
    @property
    def remaining_time(self) -> float:
        """Get remaining monitoring time in seconds."""
        return max(0, self.monitor_duration - self.elapsed_time)
    
    @property
    def is_expired(self) -> bool:
        """Check if monitoring period has expired."""
        return self.elapsed_time >= self.monitor_duration
    
    def update_metrics(self, volume_sol: float, price_sol: float) -> None:
        """Update token metrics."""
        # Track volume changes
        volume_delta = volume_sol - self.volume_sol
        if volume_delta > 0:
            self.volume_history.append((self.elapsed_time, volume_delta))
            
        # Track price history if price is available
        if price_sol > 0:
            self.price_history.append((self.elapsed_time, price_sol))
            
        self.volume_sol = volume_sol
        self.price_sol = price_sol
        self.last_check_time = monotonic()
        
    def recent_volume_acceleration(self, window_seconds: float = 5.0) -> float:
        """
        Calculate volume acceleration (change in volume rate).
        
        Args:
            window_seconds: Window size in seconds for calculation
            
        Returns:
            Volume acceleration metric (higher is stronger acceleration)
        """
        if len(self.volume_history) < 3:
            return 0.0
            
        current_time = self.elapsed_time
        recent_window = [(t, v) for t, v in self.volume_history 
                        if (current_time - t) <= window_seconds]
        older_window = [(t, v) for t, v in self.volume_history 
                       if window_seconds < (current_time - t) <= window_seconds*2]
        
        if not recent_window or not older_window:
            return 0.0
            
        recent_vol = sum(v for _, v in recent_window)
        older_vol = sum(v for _, v in older_window)
        
        # Simple acceleration metric
        acceleration = recent_vol - older_vol
        return acceleration


class PumpTrader:
    """Coordinates trading operations for pump.fun tokens with volume-based strategy."""
    def __init__(
        self,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float,
        sell_slippage: float,
        listener_type: str = "logs",
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,

        extreme_fast_mode: bool = False,
        extreme_fast_token_amount: int = 30,
        
        # Priority fee configuration
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 200_000,
        extra_priority_fee: float = 0.0,
        hard_cap_prior_fee: int = 200_000,
        
        # Retry and timeout settings
        max_retries: int = 3,
        wait_time_after_creation: int = 15, # here and further - seconds
        wait_time_after_buy: int = 15,
        wait_time_before_new_token: int = 15,
        max_token_age: int | float = 60.0,
        token_wait_timeout: int = 30,
        
        # Cleanup settings
        cleanup_mode: str = "disabled",
        cleanup_force_close_with_burn: bool = False,
        cleanup_with_priority_fee: bool = False,
        
        # Trading filters
        match_string: str | None = None,
        bro_address: str | None = None,
        marry_mode: bool = False,
        yolo_mode: bool = False,
        
        # Volume strategy settings
        volume_threshold_sol: float = 20.0,
        monitor_duration: int = 30,
        
        # Position management settings (new)
        take_profit_tiers: List[float] = None,
        sell_portions: List[float] = None, 
        stop_loss_pct: float = STOP_LOSS_PCT,
        trailing_stop_pct: float = TRAILING_STOP_PCT,
        max_positions: int = MAX_OPEN_POSITIONS,
    ):
        """Initialize the pump trader."""
        self.solana_client = SolanaClient(rpc_endpoint)
        self.wallet = Wallet(private_key)
        self.curve_manager = BondingCurveManager(self.solana_client)
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
        )
        self.buyer = TokenBuyer(
            self.solana_client,
            self.wallet,
            self.curve_manager,
            self.priority_fee_manager,
            buy_amount,
            buy_slippage,
            max_retries,
            extreme_fast_token_amount,
            extreme_fast_mode
        )
        self.seller = TokenSeller(
            self.solana_client,
            self.wallet,
            self.curve_manager,
            self.priority_fee_manager,
            sell_slippage,
            max_retries,
        )
        
        # Use our improved PumpPortal listener
        self.token_listener = PumpPortalListener()
        
        # Setup position management (new)
        if take_profit_tiers is None:
            take_profit_tiers = [1.2, 1.5, 2.0]  # Default tiers
            
        if sell_portions is None:
            sell_portions = [0.33, 0.33, 1.0]  # Default sell portions
            
        self.position_manager = PositionManager(
            take_profit_tiers=take_profit_tiers,
            sell_portions=sell_portions,
            stop_loss_pct=stop_loss_pct,
            trailing_stop_pct=trailing_stop_pct,
            max_positions=max_positions
        )
        
        # Setup analytics (new)
        self.analytics = TradingAnalytics()
        
        # Trading parameters
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount
        
        # Timing parameters
        self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy
        self.wait_time_before_new_token = wait_time_before_new_token
        self.max_token_age = max_token_age
        self.token_wait_timeout = token_wait_timeout
        
        # Cleanup parameters
        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close_with_burn = cleanup_force_close_with_burn
        self.cleanup_with_priority_fee = cleanup_with_priority_fee

        # Trading filters/modes
        self.match_string = match_string
        self.bro_address = bro_address
        self.marry_mode = marry_mode
        self.yolo_mode = yolo_mode
        
        # Volume strategy settings
        self.volume_threshold_sol = volume_threshold_sol
        self.monitor_duration = monitor_duration
        
        # State tracking
        self.traded_mints: Set[Pubkey] = set()
        self.processed_tokens: Set[str] = set()
        self.token_timestamps: Dict[str, float] = {}
        
        # Active monitoring state
        self.active_monitors: Dict[str, TokenMonitor] = {}
        self.monitoring_task = None
        self.position_monitor_task = None
        
        # Print welcome message with colored output
        print(f"{Fore.CYAN}{'='*80}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}💰 PUMP.FUN VOLUME SNIPER BOT{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'='*80}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Volume threshold: {self.volume_threshold_sol} SOL{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Monitoring duration: {self.monitor_duration} seconds{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Buy amount: {self.buy_amount} SOL{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Take profit tiers: {take_profit_tiers}{Style.RESET_ALL}")
        print(f"{Fore.GREEN}Trailing stop: {trailing_stop_pct * 100}%{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'='*80}{Style.RESET_ALL}\n")
        
    async def start(self) -> None:
        """Start the trading bot and listen for new tokens."""
        try:
            health_resp = await self.solana_client.get_health()
            print(f"{Fore.GREEN}✓ RPC connection established{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.RED}✗ RPC connection failed: {e!s}{Style.RESET_ALL}")
            return

        try:
            # Start the monitoring loops as background tasks
            self.monitoring_task = asyncio.create_task(self._monitor_tokens_loop())
            self.position_monitor_task = asyncio.create_task(self._monitor_positions_loop())
            
            # Start listening for new tokens
            print(f"{Fore.YELLOW}⚡ Starting token listener...{Style.RESET_ALL}\n")
            await self.token_listener.listen_for_tokens(
                lambda token: self._on_token_detected(token),
                self.match_string,
                self.bro_address,
            )
        
        except Exception as e:
            print(f"{Fore.RED}✗ Error: {e!s}{Style.RESET_ALL}")
        
        finally:
            if self.monitoring_task:
                self.monitoring_task.cancel()
                try:
                    await self.monitoring_task
                except asyncio.CancelledError:
                    pass
                    
            if self.position_monitor_task:
                self.position_monitor_task.cancel()
                try:
                    await self.position_monitor_task
                except asyncio.CancelledError:
                    pass
            
            await self._cleanup_resources()

    async def _cleanup_resources(self) -> None:
        """Perform cleanup operations before shutting down."""
        if self.traded_mints:
            try:
                print(f"{Fore.YELLOW}🧹 Cleaning up {len(self.traded_mints)} traded token(s)...{Style.RESET_ALL}")
                await handle_cleanup_post_session(
                    self.solana_client, 
                    self.wallet, 
                    list(self.traded_mints), 
                    self.priority_fee_manager,
                    self.cleanup_mode,
                    self.cleanup_with_priority_fee,
                    self.cleanup_force_close_with_burn
                )
            except Exception as e:
                print(f"{Fore.RED}✗ Error during cleanup: {e!s}{Style.RESET_ALL}")
                
        await self.solana_client.close()
        print(f"{Fore.YELLOW}👋 Bot shutdown complete{Style.RESET_ALL}")

    async def _on_token_detected(self, token_info: TokenInfo) -> None:
        """Handle newly detected token."""
        token_key = str(token_info.mint)
        
        if token_key in self.processed_tokens:
            return
            
        self.processed_tokens.add(token_key)
        self.token_timestamps[token_key] = monotonic()
        
        # Show minimal token info
        token_address_short = f"{token_key[:6]}...{token_key[-4:]}"
        print(f"{Fore.YELLOW}🔎 Token detected: {token_info.symbol} ({token_address_short}){Style.RESET_ALL}")
        
        # Create a new token monitor and add to active monitors
        self.active_monitors[token_key] = TokenMonitor(token_info, self.monitor_duration)

    def _display_position_summary(self):
        """Display summary of all current positions."""
        position_count = len(self.position_manager.positions)
        max_positions = self.position_manager.max_positions

        if position_count == 0:
            return

        print(f"\n{Fore.CYAN}💼 ACTIVE POSITIONS: {position_count}/{max_positions}{Style.RESET_ALL}")
        for token_key, position in self.position_manager.positions.items():
            roi = position.roi_pct
            color = Fore.GREEN if roi >= 0 else Fore.RED
            print(
                f"  {Fore.YELLOW}{position.symbol}{Style.RESET_ALL}: "
                f"Entry {position.entry_price:.8f} | ROI: {color}{roi:.2f}%{Style.RESET_ALL}"
            )
        print("")

    async def _monitor_tokens_loop(self) -> None:
        """Background task that continuously monitors active tokens."""
        status_update_interval = 8  # Reduced frequency - show updates less often
        last_status_times = {}  # Track last status update time for each token
        trade_counter = {}  # Count trades per token to reduce message frequency
        
        while True:
            try:
                # Process all active monitors
                expired_tokens = []
                
                current_time = monotonic()
                
                for token_key, monitor in list(self.active_monitors.items()):
                    if not monitor.monitoring_active:
                        continue
                        
                    token_info = monitor.token_info
                    token_address_short = f"{token_key[:6]}...{token_key[-4:]}"
                    
                    # Check if monitoring period has expired
                    if monitor.is_expired:
                        if not monitor.volume_target_reached:
                            print(f"{Fore.RED}⏱️ Timeout: {token_info.symbol} ({token_address_short}) - Volume: {monitor.volume_sol:.2f} SOL{Style.RESET_ALL}")
                            expired_tokens.append(token_key)
                            # Unsubscribe from token trades when monitoring expires
                            await self.token_listener._unsubscribe_from_token_trades(token_key)
                        continue
                    
                    # Update token metrics - get volume from PumpPortal
                    volume_sol = self.token_listener.get_token_volume(token_key)
                    
                    # Count the number of trades since last update
                    previous_volume = monitor.volume_sol
                    volume_delta = volume_sol - previous_volume
                    
                    # Only log trades if volume has increased significantly (0.5 SOL+)
                    if volume_delta >= 0.5:
                        # Update trade counter
                        trade_counter[token_key] = trade_counter.get(token_key, 0) + 1
                        
                        # Instead of showing every trade, just increase counter
                        # Original code would print: 💰 Trade: token | +X.XX SOL | Total: Y.YY SOL
                        # We're not logging this to reduce spam
                    
                    # Try to get price, but handle errors gracefully
                    try:
                        curve_state = await self.curve_manager.get_curve_state(token_info.bonding_curve)
                        price_sol = curve_state.calculate_price()
                    except Exception:
                        price_sol = 0  # Price not available
                    
                    monitor.update_metrics(volume_sol, price_sol)
                    
                    # Check if volume threshold reached
                    if monitor.volume_sol >= self.volume_threshold_sol and not monitor.volume_target_reached:
                        monitor.volume_target_reached = True
                        
                        # Check for volume acceleration (new enhancement)
                        accel = monitor.recent_volume_acceleration()
                        accel_msg = ""
                        if accel > 5:
                            accel_msg = f"{Fore.GREEN}🚀 HIGH VELOCITY (+{accel:.1f} SOL){Style.RESET_ALL}"
                        
                        print(f"{Fore.GREEN}🚀 VOLUME TARGET REACHED: {token_info.symbol} ({token_address_short}) {accel_msg}{Style.RESET_ALL}")
                        print(f"{Fore.GREEN}   Volume: {monitor.volume_sol:.2f} SOL | Time: {monitor.elapsed_time:.1f}s{Style.RESET_ALL}")
                        print(f"{Fore.YELLOW}⚡ Buying token...{Style.RESET_ALL}")
                        
                        # Check if we can open more positions
                        if not self.position_manager.has_capacity() and not self.marry_mode:
                            print(f"{Fore.YELLOW}⚠️ Max positions reached ({self.position_manager.max_positions}). Skipping {token_info.symbol}.{Style.RESET_ALL}")
                            continue
                        
                        # Buy token in a separate task to not block monitoring loop
                        asyncio.create_task(self._buy_token(token_info))
                    
                    # Only show status updates at defined intervals to reduce spam
                    last_status_time = last_status_times.get(token_key, 0)
                    if (current_time - last_status_time) >= status_update_interval and not monitor.volume_target_reached:
                        # Only show tokens with significant volume or recent activity
                        if monitor.volume_sol >= 5.0 or volume_delta > 0.5 or trade_counter.get(token_key, 0) > 3:
                            progress = int((monitor.elapsed_time / monitor.monitor_duration) * 20)
                            progress_bar = f"[{'#' * progress}{' ' * (20-progress)}]"
                            trade_count = trade_counter.get(token_key, 0)
                            
                            print(f"{Fore.CYAN}📊 {token_info.symbol}: Vol: {monitor.volume_sol:.2f} SOL | {progress_bar} {int(monitor.remaining_time)}s | {trade_count} trades{Style.RESET_ALL}")
                            
                        # Update last status time regardless of whether we printed
                        last_status_times[token_key] = current_time
                
                # Remove expired monitors
                for token_key in expired_tokens:
                    if token_key in self.active_monitors:
                        del self.active_monitors[token_key]
                        if token_key in last_status_times:
                            del last_status_times[token_key]
                        if token_key in trade_counter:
                            del trade_counter[token_key]
                
                # Small delay to avoid excessive CPU usage
                await asyncio.sleep(0.5)
                
            except asyncio.CancelledError:
                # Handle cancellation
                break
            except Exception as e:
                print(f"{Fore.RED}✗ Error in monitoring loop: {e!s}{Style.RESET_ALL}")
                await asyncio.sleep(1)
                
    async def _monitor_positions_loop(self) -> None:
        """Background task that continuously monitors position status for exit conditions."""
        print(f"{Fore.CYAN}📈 Starting position monitor...{Style.RESET_ALL}")
        
        update_interval = 5  # Show position updates every 5 seconds
        position_display_interval = 30  # Show position summary every 30 seconds  
        last_update_time = {}
        last_position_display = 0
        error_counts = {}
        
        while True:
            try:
                current_time = monotonic()
                
                # Display position summary periodically
                if (current_time - last_position_display) >= position_display_interval:
                    self._display_position_summary()
                    last_position_display = current_time
                
                # Process all active positions
                for token_key, position in list(self.position_manager.positions.items()):
                    try:
                        # Don't check price too frequently to avoid API spam
                        last_time = last_update_time.get(token_key, 0)
                        if current_time - last_time < 1.0:  # At most once per second per token
                            continue
                            
                        last_update_time[token_key] = current_time
                        
                        # Get current price with appropriate error handling
                        try:
                            curve_state = await self.curve_manager.get_curve_state(position.token_info.bonding_curve)
                            current_price = curve_state.calculate_price()
                            
                            # Reset error count on success
                            error_counts[token_key] = 0
                            
                        except Exception as e:
                            # Count consecutive errors
                            error_counts[token_key] = error_counts.get(token_key, 0) + 1
                            
                            # Only log every 10th error to avoid flooding
                            if error_counts[token_key] % 10 == 1:
                                print(f"{Fore.YELLOW}⚠️ Error getting price for {position.symbol}: Attempt {error_counts[token_key]}{Style.RESET_ALL}")
                            
                            # Skip this iteration after too many errors
                            if error_counts[token_key] > 30:  # After 30 attempts, consider removing
                                print(f"{Fore.RED}✗ Too many errors getting price for {position.symbol}. Consider manual intervention.{Style.RESET_ALL}")
                                
                            continue  # Skip to next token
                        
                        # Update position with current price
                        position.update(current_price)
                        
                        # Print position status update less frequently to reduce spam
                        if (current_time - last_time) >= update_interval:
                            # Fix the indentation issue and add the division-by-zero protection
                            if current_price <= 0.0000001:  # prevent division issues
                                roi = 0
                            else:
                                roi = (current_price / position.entry_price - 1) * 100
                                
                            roi_color = Fore.GREEN if roi >= 0 else Fore.RED
                            print(f"{Fore.CYAN}💼 {position.symbol}: Current: {current_price:.8f} | ROI: {roi_color}{roi:.2f}%{Style.RESET_ALL}")
                        
                        # Check for exit conditions only if we have a valid price
                        if current_price > 0:
                            exit_info = position.get_exit_info(current_price)
                            if exit_info:
                                print(exit_info["message"])
                                # Pass the full exit_info dict into _sell_token so we know how much to sell
                                await self._sell_token(token_key, position, exit_info)
                            
                    except Exception as e:
                        print(f"{Fore.RED}✗ Error monitoring position {position.symbol}: {e!s}{Style.RESET_ALL}")
                
                # Small delay to avoid excessive CPU usage
                await asyncio.sleep(0.5)
                    
            except asyncio.CancelledError:
                # Handle cancellation
                break
            except Exception as e:
                print(f"{Fore.RED}✗ Error in position monitoring loop: {e!s}{Style.RESET_ALL}")
                await asyncio.sleep(1)

    async def _buy_token(self, token_info: TokenInfo) -> None:
        """Buy a token that has met the volume threshold."""
        token_key = str(token_info.mint)
        token_address_short = f"{token_key[:6]}...{token_key[-4:]}"
        
        try:
            # Skip if we're at max positions and not in marry mode
            if not self.marry_mode and not self.position_manager.has_capacity():
                print(f"{Fore.YELLOW}⚠️ Max positions reached ({self.position_manager.max_positions}). Skipping {token_info.symbol}.{Style.RESET_ALL}")
                return
            
            # Update the user field before buying
            token_info.user = self.wallet.pubkey
            
            # Ensure bonding curve is initialized before buying
            for attempt in range(8):  # Increased attempts
                bonding_curve_account = await self.solana_client.get_account_info(token_info.bonding_curve)
                if bonding_curve_account is not None and bonding_curve_account.owner == PumpAddresses.PROGRAM:
                    logger.debug(f"✓ Bonding curve verified for {token_info.symbol}")
                    break
                
                wait_time = 2 + attempt  # Increasing delays
                logger.debug(f"⏳ Waiting for bonding curve initialization... (attempt {attempt+1}/8) {wait_time}s")
                await asyncio.sleep(wait_time)
                
                # If last attempt, log details
                if attempt == 7:
                    print(f"{Fore.RED}🔍 Bonding curve debug: {token_info.bonding_curve}{Style.RESET_ALL}")
            
            # Execute buy operation
            buy_result: TradeResult = await self.buyer.execute(token_info)

            if buy_result.success:
                print(f"{Fore.GREEN}✅ BOUGHT {token_info.symbol} ({token_address_short}){Style.RESET_ALL}")
                print(f"{Fore.GREEN}   Amount: {buy_result.amount:.6f} tokens | Price: {buy_result.price:.8f} SOL{Style.RESET_ALL}")
                print(f"{Fore.GREEN}   TX: {buy_result.tx_signature}{Style.RESET_ALL}")
                
                self.traded_mints.add(token_info.mint)
                self._log_trade("buy", token_info, buy_result.price, buy_result.amount, buy_result.tx_signature)
                
                if self.marry_mode:
                    # In marry mode, just hold the token
                    print(f"{Fore.YELLOW}💍 MARRY MODE: Holding {token_info.symbol} indefinitely{Style.RESET_ALL}")
                else:
                    # Create a new position and add to position manager
                    position = self.position_manager.add_position(token_info, buy_result)
                    print(f"{Fore.CYAN}📈 Position opened: {token_info.symbol} at {buy_result.price:.6f} SOL{Style.RESET_ALL}")
                    print(f"{Fore.CYAN}   Stop loss: {position.stop_price:.6f} SOL | Take profits: {[round(t*buy_result.price, 6) for t in position.take_profit_tiers]}{Style.RESET_ALL}")

                    self.analytics.log_open_position(
                         token_symbol=token_info.symbol,
                         token_address=token_key,
                         entry_price=buy_result.price,
                         amount=buy_result.amount,
                         entry_time=position.buy_time,
                         entry_tx=buy_result.tx_signature
                     )  
                    
                    # Add delay before monitoring to ensure token account is properly initialized
                    print(f"{Fore.YELLOW}⏱️ Waiting for token account initialization before monitoring...{Style.RESET_ALL}")
                    await asyncio.sleep(5)  # Wait 5 seconds before active monitoring
                    
            else:
                print(f"{Fore.RED}✗ Failed to buy {token_info.symbol}: {buy_result.error_message}{Style.RESET_ALL}")
                
        except Exception as e:
            print(f"{Fore.RED}✗ Error buying {token_info.symbol}: {e!s}{Style.RESET_ALL}")
        
        finally:
            # Remove from active monitors
            if token_key in self.active_monitors:
                self.active_monitors[token_key].monitoring_active = False

    async def _sell_token(self, token_key: str, position: Position, exit_info: dict) -> None:
        """
        Sell a token that has met exit conditions.
        exit_info includes:
          - reason: str
          - portion: float (0–1)
          - price: float (for logging)
        """
        token_info = position.token_info
        reason = exit_info["reason"]
        portion = exit_info.get("portion", 1.0)
        token_address_short = f"{token_key[:6]}...{token_key[-4:]}"

        print(f"{Fore.YELLOW}🚪 Exit: {position.symbol} due to {reason}{Style.RESET_ALL}")

        # Calculate how much to sell
        amount_to_sell = position.amount_remaining * portion

        # Execute partial or full sell
        sell_result: TradeResult = await self.seller.execute(token_info, amount=amount_to_sell)

        if not sell_result.success:
            print(f"{Fore.RED}✗ Failed to sell {position.symbol}: {sell_result.error_message}{Style.RESET_ALL}")
            return

        sold_amt = sell_result.amount
        sell_price = sell_result.price
        print(f"{Fore.GREEN}✅ SOLD {position.symbol} ({token_address_short}){Style.RESET_ALL}")
        print(f"{Fore.GREEN}   Amount: {sold_amt:.6f} | Price: {sell_price:.8f} SOL{Style.RESET_ALL}")
        print(f"{Fore.GREEN}   TX: {sell_result.tx_signature}{Style.RESET_ALL}")

        # Update realized profit and remaining amount
        profit = sold_amt * (sell_price - position.entry_price)
        position.realized_profit += profit
        position.mark_tier_taken() if portion < 1.0 else None

        # If it was a full exit (portion == 1.0), remove and log final stats
        if portion >= 1.0:
            roi = position.roi_pct
            color = Fore.GREEN if roi >= 0 else Fore.RED
            print(f"{color}   ROI: {roi:.2f}% (realized +{position.realized_profit:.6f} SOL){Style.RESET_ALL}")

            # Log and analytics
            self._log_trade("sell", token_info, sell_price, sold_amt, sell_result.tx_signature, reason)
            self.analytics.log_trade(
                token_symbol=position.symbol,
                token_address=token_key,
                entry_price=position.entry_price,
                exit_price=sell_price,
                amount=position.original_amount,
                entry_time=position.buy_time,
                exit_time=monotonic(),
                entry_tx=position.tx_signature,
                exit_tx=sell_result.tx_signature,
                exit_reason=reason
            )
            self.position_manager.remove_position(token_key)

            # Post-sell cleanup
            await handle_cleanup_after_sell(
                self.solana_client,
                self.wallet,
                token_info.mint,
                self.priority_fee_manager,
                self.cleanup_mode,
                self.cleanup_with_priority_fee,
                self.cleanup_force_close_with_burn
            )

            # Show updated analytics summary
            print(self.analytics.get_summary_text())
        else:
            # Partial sale only
            print(f"{Fore.CYAN}   Remaining: {position.amount_remaining:.6f} tokens, "
                  f"Realized profit: {position.realized_profit:.6f} SOL{Style.RESET_ALL}")

    def _log_trade(
        self,
        action: str,
        token_info: TokenInfo,
        price: float,
        amount: float,
        tx_hash: str | None,
        reason: str = "manual",
    ) -> None:
        """Log trade information."""
        try:
            os.makedirs("trades", exist_ok=True)

            log_entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "action": action,
                "token_address": str(token_info.mint),
                "symbol": token_info.symbol,
                "price": price,
                "amount": amount,
                "tx_hash": str(tx_hash) if tx_hash else None,
                "reason": reason,
            }

            with open("trades/trades.log", "a") as log_file:
                log_file.write(json.dumps(log_entry) + "\n")
        except Exception as e:
            print(f"{Fore.RED}✗ Failed to log trade information: {e!s}{Style.RESET_ALL}")
