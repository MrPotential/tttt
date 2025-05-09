"""
Monitor for token activity following creation.
"""

from time import monotonic
from solders.pubkey import Pubkey

from trading.base import TokenInfo
from utils.logger import get_logger

logger = get_logger(__name__)

class TokenMonitor:
    """Monitors token activity and tracks volume."""
    
    def __init__(self, token_info: TokenInfo, monitor_duration: int = 30):
        """
        Initialize token monitor.
        
        Args:
            token_info: Token to monitor
            monitor_duration: Duration in seconds to monitor for
        """
        self.token_info = token_info
        self.monitor_duration = monitor_duration
        self.start_time = monotonic()
        self.volume_sol = 0.0
        self.monitoring_active = True
        self.volume_target_reached = False
        self.volume_history = []  # Store (timestamp, volume_delta) tuples
        self.price_history = []   # Store (timestamp, price) tuples
    
    @property
    def elapsed_time(self) -> float:
        """Get elapsed monitoring time in seconds."""
        return monotonic() - self.start_time
    
    @property
    def is_expired(self) -> bool:
        """Check if monitoring period has expired."""
        return self.elapsed_time >= self.monitor_duration
    
    def add_volume(self, volume_delta: float) -> None:
        """Add volume from a trade event."""
        self.volume_sol += volume_delta
        self.volume_history.append((self.elapsed_time, volume_delta))
    
    def add_price(self, price: float) -> None:
        """Record a price data point."""
        self.price_history.append((self.elapsed_time, price))
    
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
            
        now = monotonic()
        recent_window = [(t, v) for t, v in self.volume_history 
                        if (now - self.start_time - t) <= window_seconds]
        older_window = [(t, v) for t, v in self.volume_history 
                       if window_seconds < (now - self.start_time - t) <= window_seconds*2]
        
        if not recent_window or not older_window:
            return 0.0
            
        recent_vol = sum(v for _, v in recent_window)
        older_vol = sum(v for _, v in older_window)
        
        # Simple acceleration metric
        acceleration = recent_vol - older_vol
        return acceleration
