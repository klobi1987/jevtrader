"""JevTrader CLI Entrypoint.

Usage:
    python run.py --dry-run   # Live data + real AI decisions, simulated orders
    python run.py --live      # Live order execution on Bybit with real capital
"""

import sys
import argparse
import logging
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev.config import load_config
from jev.engine import JevTraderEngine

def setup_logging():
    # Fix Windows CP1252 console encoding
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("data/jevtrader.log", encoding="utf-8")
        ]
    )

def main():
    parser = argparse.ArgumentParser(description="JevTrader - High-Frequency Autonomous Crypto Perpetual Engine")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Run in dry-run simulation mode")
    parser.add_argument("--live", action="store_true", default=False, help="Run in live trading mode on Bybit")
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to config.yaml")

    args = parser.parse_args()

    # Default to dry-run if neither specified for safety
    is_dry_run = not args.live

    Path("data").mkdir(exist_ok=True)
    setup_logging()
    logger = logging.getLogger("jevtrader.cli")

    mode_str = "LIVE REAL MONEY TRADING" if not is_dry_run else "DRY-RUN SIMULATION (Paper Trading)"
    logger.info("=" * 70)
    logger.info(f"   STARTING JEVTRADER APEX - {mode_str}")
    logger.info("=" * 70)

    try:
        config = load_config(args.config)
        engine = JevTraderEngine(config=config, dry_run=is_dry_run)
        engine.start()
    except KeyboardInterrupt:
        logger.info("JevTrader stopped by user.")
    except Exception as e:
        logger.critical(f"Fatal error in JevTrader: {e}", exc_info=True)

if __name__ == "__main__":
    main()
