import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import shutil
import tempfile
from jev.contracts import ActivePosition, TradeDirection, TradeStatus
from jev.positions import PositionManager
from jev.store import StateStore

class MockBybit:
    def __init__(self):
        self.orders = []
        self.trading_stops = []

    def place_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"retCode": 0, "orderId": "mock_order_1"}

    def set_trading_stop(self, **kwargs):
        self.trading_stops.append(kwargs)
        return {"retCode": 0, "retMsg": "OK"}

    def get_orderbook(self, symbol, limit=20):
        return {"b": [["100.0", "10.0"]], "a": [["100.1", "10.0"]]}

class MockDecisions:
    def evaluate_invalidation(self, *args, **kwargs):
        return False, 0.1

def test_ratchet_flow():
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.sqlite3")
    store = StateStore(db_path=db_path)
    client = MockBybit()
    decisions = MockDecisions()

    realized_events = []
    def on_pnl(tid, pnl, reason):
        realized_events.append((tid, pnl, reason))

    pm = PositionManager(
        client=client,
        decisions=decisions,
        store=store,
        dry_run=False,
        exit_mode="ratchet_trail",
        on_pnl_realized=on_pnl
    )

    # Entry: LONG @ 100.0, SL @ 99.0 (1R = 1.0), TP1 @ 101.5 (+1.5R), TP2 @ 105.0
    pos = ActivePosition(
        trade_id="trade_test_1",
        symbol="BTCUSDT",
        direction=TradeDirection.LONG,
        entry_price=100.0,
        current_price=100.0,
        initial_qty=1.0,
        remaining_qty=1.0,
        stop_loss_price=99.0,
        tp1_price=101.5,
        tp2_price=105.0,
        initial_risk_usd=1.0,
        status=TradeStatus.OPEN
    )
    pm.add_position(pos)
    store.create_trade("trade_test_1", "BTCUSDT", "LONG", 100.0, 99.0, 101.5, 105.0, 1.0, 1.0)

    # 1. Price moves to 101.6 -> Hits TP1
    closed = pm.update_and_watch({"BTCUSDT": {"lastPrice": "101.6"}})
    assert not closed, "Position should still have 50% runner open"
    assert pos.is_tp1_filled is True, "TP1 should be filled"
    assert pos.is_be_locked is True, "BE should be locked"
    assert pos.remaining_qty == 0.5, "Remaining qty should be 0.5"
    assert pos.stop_loss_price > 100.0, "SL should be at BE with fee buffer"
    assert len(realized_events) == 1, "Should have 1 realized PnL event for TP1"

    # 2. Price climbs to 102.1 (+2.1R) -> Should ratchet SL to +1.0R (101.0)
    closed = pm.update_and_watch({"BTCUSDT": {"lastPrice": "102.1"}})
    assert pos.ratchet_level == 1, f"Expected ratchet_level 1, got {pos.ratchet_level}"
    assert abs(pos.stop_loss_price - 101.0) < 0.001, f"Expected SL ~ 101.0, got {pos.stop_loss_price}"

    # 3. Price climbs to 103.2 (+3.2R) -> Should ratchet SL to +2.0R (102.0)
    closed = pm.update_and_watch({"BTCUSDT": {"lastPrice": "103.2"}})
    assert pos.ratchet_level == 2, f"Expected ratchet_level 2, got {pos.ratchet_level}"
    assert abs(pos.stop_loss_price - 102.0) < 0.001, f"Expected SL ~ 102.0, got {pos.stop_loss_price}"

    # 4. Price pulls back to 101.9 -> Hits Ratchet SL at 102.0
    closed = pm.update_and_watch({"BTCUSDT": {"lastPrice": "101.9"}})
    assert "trade_test_1" in closed, "Trade should be closed by Ratchet SL"
    assert pos.status == TradeStatus.CLOSED_RATCHET, f"Expected CLOSED_RATCHET, got {pos.status}"
    assert len(realized_events) == 2, "Should have realized PnL for ratchet close"
    pnl_runner = realized_events[1][1]
    assert pnl_runner > 0, f"Runner PnL must be strictly positive, got {pnl_runner}"

    # 5. Test SQLite Rehydration
    store.create_trade("trade_rehydrate", "ETHUSDT", "LONG", 2000.0, 1980.0, 2030.0, 2100.0, 0.5, 10.0)
    store.update_trade_be("trade_rehydrate")
    store.update_trade_sl("trade_rehydrate", 2020.0, "RATCHET_LOCK_1R")

    pm2 = PositionManager(client=client, decisions=decisions, store=store, dry_run=False)
    pm2.rehydrate_from_store()
    assert "trade_rehydrate" in pm2.active_positions
    rehydrated_pos = pm2.active_positions["trade_rehydrate"]
    assert rehydrated_pos.is_be_locked is True
    assert rehydrated_pos.stop_loss_price == 2020.0
    assert rehydrated_pos.ratchet_level == 1, f"Expected rehydrated ratchet level 1, got {rehydrated_pos.ratchet_level}"

    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("ALL RATCHET TRAILING TESTS PASSED PERFECTLY!")

if __name__ == "__main__":
    test_ratchet_flow()
