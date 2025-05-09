from dataclasses import dataclass
from typing import Final

from solders.pubkey import Pubkey

LAMPORTS_PER_SOL: Final[int] = 1_000_000_000
TOKEN_DECIMALS: Final[int] = 6


@dataclass
class SystemAddresses:
    """System-level Solana addresses."""

    PROGRAM: Final[Pubkey] = Pubkey.from_string("11111111111111111111111111111111")
    TOKEN_PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    )
    ASSOCIATED_TOKEN_PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
    )
    RENT: Final[Pubkey] = Pubkey.from_string(
        "SysvarRent111111111111111111111111111111111"
    )
    SOL: Final[Pubkey] = Pubkey.from_string(
        "So11111111111111111111111111111111111111112"
    )


@dataclass
class PumpAddresses:
    """Pump.fun program addresses."""

    PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    )
    GLOBAL: Final[Pubkey] = Pubkey.from_string(
        "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
    )
    EVENT_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
        "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
    )
    FEE: Final[Pubkey] = Pubkey.from_string(
        "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"
    )
    LIQUIDITY_MIGRATOR: Final[Pubkey] = Pubkey.from_string(
        "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
    )

    @staticmethod
    def get_bonding_curve_address(mint_address: Pubkey) -> Pubkey:
        """Get the PDA for the bonding curve of a given token mint."""
        seeds = [b"bonding-curve", bytes(mint_address)]
        return Pubkey.find_program_address(
            seeds,
            PumpAddresses.PROGRAM
        )[0]

    @staticmethod
    def get_associated_bonding_curve_address(
        bonding_curve: Pubkey,
        mint_address: Pubkey
    ) -> Pubkey:
        """Get the SPL associated token account PDA for the bonding curve PDA."""
        seeds = [
            bytes(bonding_curve),
            bytes(SystemAddresses.TOKEN_PROGRAM),
            bytes(mint_address),
        ]
        return Pubkey.find_program_address(
            seeds,
            SystemAddresses.ASSOCIATED_TOKEN_PROGRAM
        )[0]
