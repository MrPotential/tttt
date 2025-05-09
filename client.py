"""
Solana client abstraction for blockchain operations.
"""

import asyncio
import json
from typing import Any

import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from utils.logger import get_logger

logger = get_logger(__name__)


class SolanaClient:
    """Abstraction for Solana RPC client operations."""

    def __init__(self, rpc_endpoint: str):
        """Initialize Solana client with RPC endpoint.

        Args:
            rpc_endpoint: URL of the Solana RPC endpoint
        """
        self.rpc_endpoint = rpc_endpoint
        self._client = None
        self._cached_blockhash: Hash | None = None
        self._blockhash_last_updated = 0
        self._blockhash_lock = asyncio.Lock()
        self._blockhash_updater_task = asyncio.create_task(self.start_blockhash_updater())

    async def start_blockhash_updater(self, interval: float = 3.0):
        """Start background task to update recent blockhash.
        
        Uses a shorter interval (3s) to ensure fresher blockhashes.
        """
        while True:
            try:
                blockhash = await self.get_latest_blockhash()
                async with self._blockhash_lock:
                    self._cached_blockhash = blockhash
                    self._blockhash_last_updated = asyncio.get_event_loop().time()
            except Exception as e:
                logger.warning(f"Blockhash fetch failed: {e!s}")
            finally:
                await asyncio.sleep(interval)

    async def get_cached_blockhash(self) -> Hash:
        """Return the most recently cached blockhash.
        
        Forces a refresh if the cached blockhash is older than 10 seconds.
        """
        async with self._blockhash_lock:
            current_time = asyncio.get_event_loop().time()
            if self._cached_blockhash is None or (current_time - self._blockhash_last_updated) > 10:
                # Cached blockhash is too old or doesn't exist, get a fresh one
                try:
                    self._cached_blockhash = await self.get_latest_blockhash()
                    self._blockhash_last_updated = current_time
                except Exception as e:
                    if self._cached_blockhash is None:
                        raise RuntimeError(f"Failed to get fresh blockhash: {e!s}")
                    logger.warning(f"Failed to refresh blockhash, using existing one: {e!s}")
            
            return self._cached_blockhash

    async def get_client(self) -> AsyncClient:
        """Get or create the AsyncClient instance.

        Returns:
            AsyncClient instance
        """
        if self._client is None:
            self._client = AsyncClient(self.rpc_endpoint)
        return self._client

    async def close(self):
        """Close the client connection and stop the blockhash updater."""
        if self._blockhash_updater_task:
            self._blockhash_updater_task.cancel()
            try:
                await self._blockhash_updater_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.close()
            self._client = None

    async def get_health(self) -> str | None:
        """Check RPC node health.
        
        Returns:
            Health status string or None if request failed
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getHealth",
        }
        result = await self.post_rpc(body)
        if result and "result" in result:
            return result["result"]
        return None

    async def get_account_info(self, pubkey: Pubkey) -> dict[str, Any]:
        """Get account info from the blockchain.

        Args:
            pubkey: Public key of the account

        Returns:
            Account info response

        Raises:
            ValueError: If account doesn't exist or has no data
        """
        client = await self.get_client()
        response = await client.get_account_info(pubkey, encoding="base64") # base64 encoding for account data by default
        if not response.value:
            return None  # Return None instead of raising an exception for non-existent accounts
        return response.value

    async def get_token_account_balance(self, token_account: Pubkey) -> int:
        """Get token balance for an account.

        Args:
            token_account: Token account address

        Returns:
            Token balance as integer
        """
        client = await self.get_client()
        try:
            response = await client.get_token_account_balance(token_account)
            if response.value:
                return int(response.value.amount)
            return 0
        except Exception as e:
            logger.error(f"Failed to get token balance for {token_account}: {e!s}")
            return 0

    async def get_latest_blockhash(self) -> Hash:
        """Get the latest blockhash.

        Returns:
            Recent blockhash as string
        """
        client = await self.get_client()
        response = await client.get_latest_blockhash(commitment="processed")
        return response.value.blockhash

    async def build_and_send_transaction(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        skip_preflight: bool = True,
        max_retries: int = 3,
        priority_fee: int | None = None,
    ) -> str:
        """
        Send a transaction with optional priority fee.

        Args:
            instructions: List of instructions to include in the transaction.
            skip_preflight: Whether to skip preflight checks.
            max_retries: Maximum number of retry attempts.
            priority_fee: Optional priority fee in microlamports.

        Returns:
            Transaction signature.
            
        Raises:
            Exception: With detailed error information if transaction fails
        """
        client = await self.get_client()

        # Add priority fee instructions if applicable
        if priority_fee is not None:
            logger.info(f"Priority fee in microlamports: {priority_fee}")
            
            # Compute unit instructions always come first
            compute_units = 100_000  # Increased compute unit limit to handle complex transactions
            fee_instructions = [
                set_compute_unit_limit(compute_units),
                set_compute_unit_price(priority_fee),
            ]
            instructions = fee_instructions + instructions
        else:
            logger.info("No priority fee specified")

        for attempt in range(max_retries):
            try:
                # Always get a fresh blockhash for each attempt
                recent_blockhash = await self.get_cached_blockhash()
                
                message = Message(instructions, signer_keypair.pubkey())
                transaction = Transaction([signer_keypair], message, recent_blockhash)

                tx_opts = TxOpts(
                    skip_preflight=skip_preflight, 
                    preflight_commitment=Processed
                )
                
                logger.debug(f"Sending transaction attempt {attempt + 1}/{max_retries}")
                
                # Send the transaction
                response = await client.send_transaction(transaction, tx_opts)
                
                # Check for errors in the response
                if hasattr(response, 'error') and response.error:
                    error_msg = response.error.get('message', 'Unknown error')
                    logger.error(f"Transaction error: {error_msg}")
                    raise RuntimeError(f"Transaction failed: {error_msg}")
                
                # Log transaction details
                tx_sig = response.value
                logger.info(f"Transaction sent: {tx_sig}")
                return tx_sig

            except Exception as e:
                # Format the error message details
                error_msg = str(e)
                
                # Try to extract JSON error details if present
                try:
                    if hasattr(e, 'args') and len(e.args) > 0:
                        if isinstance(e.args[0], str) and e.args[0].find('{') >= 0:
                            json_start = e.args[0].find('{')
                            json_str = e.args[0][json_start:]
                            error_details = json.loads(json_str)
                            if isinstance(error_details, dict) and 'error' in error_details:
                                error_msg = f"{error_details['error'].get('message', 'Unknown error')}"
                except Exception as json_e:
                    logger.debug(f"Failed to parse error JSON: {json_e}")
                
                if attempt == max_retries - 1:
                    logger.error(f"Failed to send transaction after {max_retries} attempts: {error_msg}")
                    # Include the full error details when raising the exception
                    raise RuntimeError(f"{error_msg}")

                wait_time = 2**attempt
                logger.warning(
                    f"Transaction attempt {attempt + 1} failed: {error_msg}, retrying in {wait_time}s"
                )
                await asyncio.sleep(wait_time)

    async def confirm_transaction(
        self, signature: str, commitment: str = "confirmed", timeout: int = 60
    ) -> bool:
        """Wait for transaction confirmation.

        Args:
            signature: Transaction signature
            commitment: Confirmation commitment level
            timeout: Maximum time to wait for confirmation (seconds)

        Returns:
            Whether transaction was confirmed
        """
        client = await self.get_client()
        try:
            # Use a timeout to prevent waiting indefinitely
            await asyncio.wait_for(
                client.confirm_transaction(
                    signature, 
                    commitment=commitment, 
                    sleep_seconds=1
                ),
                timeout=timeout
            )
            logger.info(f"Transaction {signature} confirmed")
            return True
        except asyncio.TimeoutError:
            logger.error(f"Transaction {signature} confirmation timed out after {timeout}s")
            return False
        except Exception as e:
            # Log the detailed error message
            logger.error(f"Failed to confirm transaction {signature}: {e!s}")
            return False

    async def post_rpc(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """
        Send a raw RPC request to the Solana node.

        Args:
            body: JSON-RPC request body.

        Returns:
            Optional[Dict[str, Any]]: Parsed JSON response, or None if the request fails.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.rpc_endpoint,
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),  # Increase timeout to 15s
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
                    
                    # Check for RPC errors in the response
                    if "error" in result:
                        error_msg = result["error"].get("message", "Unknown RPC error")
                        logger.error(f"RPC error: {error_msg}")
                    
                    return result
                    
        except aiohttp.ClientError as e:
            logger.error(f"RPC request failed: {e!s}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode RPC response: {e!s}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during RPC request: {e!s}")
            return None
