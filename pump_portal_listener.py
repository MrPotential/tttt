"""
Listener for PumpPortal real-time token creation events.
"""

import asyncio
import json
import websockets
from solders.pubkey import Pubkey

from monitoring.base_listener import BaseTokenListener
from trading.base import TokenInfo
from utils.logger import get_logger

logger = get_logger(__name__)

class PumpPortalListener(BaseTokenListener):
    """WebSocket listener for pump.fun token creation events using PumpPortal API."""
    
    def __init__(self, volume_threshold: float = 20.0, token_wait_timeout: float = 30.0):
        """
        Initialize the PumpPortal listener.
        
        Args:
            volume_threshold: Volume threshold in SOL to trigger buy.
            token_wait_timeout: Time window (seconds) to wait for volume threshold.
        """
        self.url = "wss://pumpportal.fun/api/data"
        self.volume_threshold = volume_threshold
        self.token_wait_timeout = token_wait_timeout
        self.active = False
        # Internal state tracking
        self.websocket = None
        self.subscribed_tokens = set()
        self.token_volumes = {}
        self.pending_tokens = {}
        self.threshold_tasks = {}
        logger.info(f"PumpPortal listener initialized (threshold={volume_threshold} SOL, window={token_wait_timeout}s)")
    
    async def listen_for_tokens(self, token_callback, match_string=None, creator_address=None):
        """
        Listen for new token creation events and trigger callback when conditions are met.
        
        Args:
            token_callback: Coroutine function to call with TokenInfo when buy condition is met.
            match_string: Optional substring to filter token name/symbol.
            creator_address: Optional creator address to filter tokens.
        """
        self.active = True
        while self.active:
            try:
                async with websockets.connect(self.url) as websocket:
                    self.websocket = websocket
                    logger.info(f"Connected to PumpPortal WebSocket at {self.url}")
                    # Subscribe to new token events
                    subscribe_msg = {"method": "subscribeNewToken"}
                    await websocket.send(json.dumps(subscribe_msg))
                    try:
                        init_resp = await asyncio.wait_for(websocket.recv(), timeout=2)
                        logger.debug(f"PumpPortal init response: {init_resp}")
                    except asyncio.TimeoutError:
                        # No initial response needed
                        pass
                    # Listen for messages
                    while self.active:
                        try:
                            message = await websocket.recv()
                        except websockets.exceptions.ConnectionClosed:
                            logger.warning("PumpPortal WebSocket connection closed, reconnecting...")
                            break
                        except Exception as e:
                            logger.error(f"Error receiving PumpPortal message: {e}")
                            break
                        # Process incoming message
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            logger.error("Received invalid JSON on PumpPortal WebSocket")
                            continue
                        try:
                            # Check for token creation event
                            token_data = None
                            if isinstance(data, dict):
                                if data.get("type") == "token_create" and "data" in data:
                                    token_data = data["data"]
                                elif "mint" in data and "name" in data:
                                    token_data = data
                            if token_data and "mint" in token_data:
                                mint_str = token_data.get("mint")
                                name = token_data.get("name", "") or ""
                                symbol = token_data.get("symbol", "") or ""
                                creator = token_data.get("creator") or token_data.get("traderPublicKey", "")
                                logger.info(f"New token detected: {name} ({symbol}) - {mint_str}")
                                # Apply filters
                                if match_string and match_string.lower() not in (name + symbol).lower():
                                    logger.info(f"Token {symbol} doesn't match filter '{match_string}'. Skipping.")
                                    continue
                                if creator_address and creator != creator_address:
                                    logger.info(f"Token {symbol} creator {creator} != {creator_address}. Skipping.")
                                    continue
                                # Derive necessary addresses and prepare TokenInfo
                                try:
                                    mint = Pubkey.from_string(mint_str)
                                    seeds = [b"bonding_curve", bytes(mint)]
                                    bonding_curve, _ = Pubkey.find_program_address(seeds, Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"))
                                    assoc_seeds = [bytes(bonding_curve), b"\x01", bytes(mint)]
                                    associated_bc, _ = Pubkey.find_program_address(assoc_seeds, Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"))
                                    token_info = TokenInfo(
                                        mint=mint,
                                        name=name,
                                        symbol=symbol,
                                        bonding_curve=bonding_curve,
                                        associated_bonding_curve=associated_bc,
                                        user=Pubkey.from_string(creator) if creator else None,
                                        uri=token_data.get("uri", "")
                                    )
                                except Exception as e:
                                    logger.error(f"Error deriving addresses for token {symbol}: {e}")
                                    continue
                                # Initialize volume tracking and subscribe to trades for this token
                                self.token_volumes[mint_str] = 0.0
                                self.pending_tokens[mint_str] = token_info
                                if mint_str not in self.subscribed_tokens:
                                    sub_msg = {"method": "subscribeTokenTrade", "keys": [mint_str]}
                                    try:
                                        await websocket.send(json.dumps(sub_msg))
                                        self.subscribed_tokens.add(mint_str)
                                        logger.info(f"Subscribed to trade events for token {symbol} ({mint_str})")
                                    except Exception as e:
                                        logger.error(f"Failed to subscribe to trades for token {symbol}: {e}")
                                # Start timeout for volume threshold
                                self.threshold_tasks[mint_str] = asyncio.create_task(self._volume_timeout(mint_str))
                            # Check for trade event
                            elif isinstance(data, dict) and data.get("txType") in ["buy", "sell"]:
                                token_mint = data.get("mint")
                                if not token_mint:
                                    continue
                                if token_mint in self.subscribed_tokens:
                                    sol_amount = float(data.get("solAmount", 0))
                                    self.token_volumes[token_mint] = self.token_volumes.get(token_mint, 0.0) + sol_amount
                                    mint_short = token_mint[:6] + "..." + token_mint[-4:]
                                    logger.debug(f"Trade event for {mint_short}: +{sol_amount:.2f} SOL (Total {self.token_volumes[token_mint]:.2f} SOL)")
                                    # If token is pending and threshold reached, trigger callback
                                    if token_mint in self.pending_tokens and self.token_volumes[token_mint] >= self.volume_threshold:
                                        token_info = self.pending_tokens.pop(token_mint)
                                        if token_mint in self.threshold_tasks:
                                            task = self.threshold_tasks.pop(token_mint)
                                            task.cancel()
                                        logger.info(f"Volume threshold reached for token {token_info.symbol} ({token_mint}): {self.token_volumes[token_mint]:.2f} SOL")
                                        await token_callback(token_info)
                            # Ignore other messages
                        except Exception as e:
                            logger.error(f"Error processing PumpPortal message: {e}")
                            continue
                # End of with (connection closed)
                self.websocket = None
            except Exception as e:
                logger.error(f"PumpPortal connection error: {e}")
            if self.active:
                logger.info("Reconnecting to PumpPortal WebSocket in 5 seconds...")
                await asyncio.sleep(5)
        logger.info("PumpPortal listener stopped.")
    
    async def _volume_timeout(self, token_mint: str):
        """Internal coroutine to handle volume threshold timeout."""
        try:
            await asyncio.sleep(self.token_wait_timeout)
        except asyncio.CancelledError:
            return
        if token_mint in self.pending_tokens:
            token_info = self.pending_tokens.pop(token_mint)
            symbol = token_info.symbol
            volume = self.token_volumes.get(token_mint, 0.0)
            logger.info(f"Volume threshold not met for token {symbol} ({token_mint}) in time. Skipping token (vol={volume:.2f} SOL).")
            await self.unsubscribe_token(token_mint)
    
    async def unsubscribe_token(self, token_mint: str):
        """
        Unsubscribe from trade events for a specific token.
        
        Args:
            token_mint: Mint address (string) of token to unsubscribe.
        """
        if token_mint not in self.subscribed_tokens:
            return
        unsub_msg = {"method": "unsubscribeTokenTrade", "keys": [token_mint]}
        try:
            if self.websocket:
                await self.websocket.send(json.dumps(unsub_msg))
        except Exception as e:
            logger.error(f"Error sending unsubscribe for token {token_mint}: {e}")
        self.subscribed_tokens.discard(token_mint)
        self.token_volumes.pop(token_mint, None)
        self.pending_tokens.pop(token_mint, None)
        if token_mint in self.threshold_tasks:
            task = self.threshold_tasks.pop(token_mint)
            task.cancel()
        logger.info(f"Unsubscribed from trades for token {token_mint}")
    
    def stop(self):
        """Stop the listener."""
        self.active = False
        logger.info("PumpPortal listener stopping...")
