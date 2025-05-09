import streamlit as st
import pandas as pd
import json
import time
from pathlib import Path

# Streamlit page configuration
st.set_page_config(
    page_title="Pump.fun Bot Dashboard",
    layout="wide"
)

# Paths to analytics data
DATA_DIR = Path(__file__).parent / "analytics"
TRADES_PATH = DATA_DIR / "trades.json"
POSITIONS_PATH = DATA_DIR / "positions.json"
METRICS_PATH = DATA_DIR / "metrics.json"

PLACEHOLDER = st.empty()

# Utility to load JSON safely
def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return [] if path.name != "metrics.json" else {}

# Main loop: refresh every second
while True:
    # Load data
    trades = load_json(TRADES_PATH)
    positions = load_json(POSITIONS_PATH)
    metrics = load_json(METRICS_PATH)

    # Convert to DataFrames
    df_trades = pd.DataFrame(trades)
    df_positions = pd.DataFrame(positions)

    with PLACEHOLDER.container():
        st.title("Pump.fun Trading Bot Dashboard")

        # Display key metrics
        st.subheader("🛠 Metrics")
        if metrics:
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Win Rate", f"{metrics.get('win_rate', 0) * 100:.1f}%")
            col2.metric("Total PnL (SOL)", f"{metrics.get('total_pnl_sol', 0):.4f}")
            col3.metric("Current Equity", f"{metrics.get('current_equity', 0):.4f}")
            col4.metric("Max Drawdown", f"{metrics.get('max_drawdown_pct', 0):.2f}%")
        else:
            st.info("No metrics available yet.")

        # Open Positions table
        st.subheader("📊 Open Positions")
        if not df_positions.empty:
            st.dataframe(df_positions)
        else:
            st.info("No open positions at the moment.")

        # Trade History table
        st.subheader("📈 Trade History")
        if not df_trades.empty:
            st.dataframe(df_trades)
        else:
            st.info("No trades recorded yet.")

    time.sleep(1)
