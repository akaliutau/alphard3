from core.models import Decision, SymbolInfo, Tick
from core.strategy import parse_strategy_output
from core.risk import RiskEngine


def test_parse_json_decision():
    d = parse_strategy_output('{"decision":"BUY","allocation":0.4,"confidence":0.7,"stop_loss":1.1,"take_profit":1.3,"rationale":"breakout"}')
    assert d.status == "BUY"
    assert d.stop_loss == 1.1
    assert d.take_profit == 1.3


def test_risk_approves_basic_buy(monkeypatch):
    d = Decision(status="BUY", allocation=0.5, confidence=0.9, stop_loss=1.09, take_profit=1.2)
    tick = Tick(bid=1.1, ask=1.1002)
    info = SymbolInfo(name="EURUSD", digits=5, point=0.00001, volume_min=0.01, volume_step=0.01)
    r = RiskEngine().validate(d, tick, info, positions=[], orders=[])
    assert r.approved
    assert r.volume >= 0.01


def test_risk_adjusts_invalid_sell_limit_entry_above_ask(monkeypatch):
    d = Decision(status="SELL", allocation=-0.5, confidence=0.9, stop_loss=160.31, take_profit=160.12, entry_price=160.22)
    tick = Tick(bid=160.24, ask=160.25)
    info = SymbolInfo(name="USDJPY", digits=3, point=0.001, volume_min=0.01, volume_step=0.01)

    r = RiskEngine().validate(d, tick, info, positions=[], orders=[])

    assert r.approved
    assert r.entry_price is not None
    assert r.entry_price > tick.ask
    assert d.take_profit < r.entry_price < d.stop_loss


def test_risk_autocorrects_buy_stop_loss_too_close_to_entry():
    d = Decision(
        status="BUY",
        allocation=0.5,
        confidence=0.9,
        stop_loss=1.10015,
        take_profit=1.10100,
        levels={"support": [1.09980], "resistance": [1.10100]},
    )
    tick = Tick(bid=1.10020, ask=1.10022)
    info = SymbolInfo(name="EURUSD", digits=5, point=0.00001, volume_min=0.01, volume_step=0.01)

    r = RiskEngine().validate(d, tick, info, positions=[], orders=[])

    assert r.approved
    assert r.entry_price is not None
    assert d.stop_loss < r.entry_price
    assert abs(r.entry_price - d.stop_loss) >= 20 * info.point
    assert r.adjusted.get("sl_adjustment")


def test_volume_scales_with_confidence_for_same_allocation():
    tick = Tick(bid=1.10000, ask=1.10002)
    info = SymbolInfo(name="EURUSD", digits=5, point=0.00001, volume_min=0.01, volume_step=0.01)
    low = Decision(status="BUY", allocation=0.5, confidence=0.65, stop_loss=1.09900, take_profit=1.10200)
    high = Decision(status="BUY", allocation=0.5, confidence=0.95, stop_loss=1.09900, take_profit=1.10200)

    low_risk = RiskEngine().validate(low, tick, info, positions=[], orders=[])
    high_risk = RiskEngine().validate(high, tick, info, positions=[], orders=[])

    assert low_risk.approved
    assert high_risk.approved
    assert high_risk.volume > low_risk.volume
    assert high_risk.adjusted["sizing"]["confidence_weight"] > low_risk.adjusted["sizing"]["confidence_weight"]


def test_risk_repairs_poor_reward_risk():
    d = Decision(status="BUY", allocation=0.5, confidence=0.9, stop_loss=1.09950, take_profit=1.10040)
    tick = Tick(bid=1.10000, ask=1.10002)
    info = SymbolInfo(name="EURUSD", digits=5, point=0.00001, volume_min=0.01, volume_step=0.01)

    r = RiskEngine().validate(d, tick, info, positions=[], orders=[])

    assert r.approved
    assert r.adjusted.get("tp_adjustment")


def test_xauusd_confidence_changes_symbol_capped_volume():
    tick = Tick(bid=2350.00, ask=2350.01)
    info = SymbolInfo(name="XAUUSD", digits=2, point=0.01, volume_min=0.001, volume_step=0.001)
    low = Decision(status="BUY", allocation=0.5, confidence=0.65, stop_loss=2349.50, take_profit=2351.00)
    high = Decision(status="BUY", allocation=0.5, confidence=0.9, stop_loss=2349.50, take_profit=2351.00)

    low_risk = RiskEngine().validate(low, tick, info, positions=[], orders=[])
    high_risk = RiskEngine().validate(high, tick, info, positions=[], orders=[])

    assert low_risk.approved
    assert high_risk.approved
    assert high_risk.volume > low_risk.volume
    assert high_risk.adjusted["sizing"]["volume_cap"] == 1.0


def test_cleanup_pending_orders_is_best_effort(monkeypatch, tmp_path):
    import asyncio
    from core.execution import ExecutionEngine
    from core.ledger import Ledger
    from utilities.settings import config

    class BrokenOrdersApi:
        async def orders(self, symbol=None):
            raise RuntimeError("orders endpoint down")

    old_cancel = config.cancel_stale_pending_orders
    object.__setattr__(config, "cancel_stale_pending_orders", True)
    try:
        engine = ExecutionEngine(BrokenOrdersApi(), Ledger(tmp_path / "cleanup.sqlite3"))
        result = asyncio.run(engine.cleanup_pending_orders("XAUUSD"))
    finally:
        object.__setattr__(config, "cancel_stale_pending_orders", old_cancel)

    assert result
    assert result[0]["ok"] is False
    assert result[0]["stage"] == "list_orders"


def test_execute_logs_failed_mt5_call_in_ledger(monkeypatch, tmp_path):
    import asyncio
    from core.execution import ExecutionEngine
    from core.ledger import Ledger, EventType
    from core.models import RiskResult
    from utilities.settings import config

    class BrokenTradeApi:
        async def place_pending_order(self, body):
            raise RuntimeError("pending endpoint down")

        async def open_deal(self, body):
            raise AssertionError("market mode not expected")

    old_dry_run = config.dry_run
    old_execution_mode = config.execution_mode
    old_timeframe = config.timeframe
    object.__setattr__(config, "dry_run", False)
    object.__setattr__(config, "execution_mode", "pending_limit")
    object.__setattr__(config, "timeframe", "M1")
    try:
        ledger = Ledger(tmp_path / "execute.sqlite3")
        engine = ExecutionEngine(BrokenTradeApi(), ledger)
        decision = Decision(status="SELL", allocation=-0.5, confidence=0.9, stop_loss=4168.5, take_profit=4156.5)
        risk = RiskResult(approved=True, reason="approved", volume=0.1, entry_price=4164.6)
        tick = Tick(bid=4164.0, ask=4164.5)
        info = SymbolInfo(name="XAUUSD", digits=2, point=0.01, volume_min=0.01, volume_step=0.01)
        result = asyncio.run(engine.execute("XAUUSD", 202606191621, "levels_strategy", decision, risk, tick, info))
    finally:
        object.__setattr__(config, "dry_run", old_dry_run)
        object.__setattr__(config, "execution_mode", old_execution_mode)
        object.__setattr__(config, "timeframe", old_timeframe)

    assert result.attempted
    assert not result.ok
    assert "pending endpoint down" in str(result.error)
    event = ledger.get_last_event(EventType.TRADE, "XAUUSD", 202606191621, "levels_strategy")
    assert event is not None
    assert event["attempted"] is True
    assert event["ok"] is False


def test_risk_autocorrects_reward_risk_by_extending_take_profit():
    d = Decision(status="BUY", allocation=0.5, confidence=0.9, stop_loss=1.09950, take_profit=1.10040)
    tick = Tick(bid=1.10000, ask=1.10002)
    info = SymbolInfo(name="EURUSD", digits=5, point=0.00001, volume_min=0.01, volume_step=0.01)

    r = RiskEngine().validate(d, tick, info, positions=[], orders=[])

    assert r.approved
    assert d.take_profit > 1.10040
    assert r.adjusted.get("tp_adjustment")


def test_risk_autocorrects_sell_limit_stop_too_close_to_entry():
    d = Decision(status="SELL", allocation=-0.5, confidence=0.75, stop_loss=0.86703, take_profit=0.86666)
    tick = Tick(bid=0.86670, ask=0.86686)
    info = SymbolInfo(name="EURGBP", digits=5, point=0.00001, volume_min=0.01, volume_step=0.01)

    r = RiskEngine().validate(d, tick, info, positions=[], orders=[])

    assert r.approved
    assert r.entry_price is not None
    assert d.stop_loss - r.entry_price >= 20 * info.point
    assert d.take_profit < r.entry_price
    assert r.adjusted.get("sl_adjustment")


def test_execution_reprices_pending_order_from_fresh_tick_and_omits_comment(tmp_path):
    import asyncio
    from core.execution import ExecutionEngine
    from core.ledger import Ledger
    from core.models import RiskResult
    from utilities.settings import config

    class Api:
        def __init__(self):
            self.sent = []

        async def tick(self, symbol):
            return (
                Tick(bid=4159.10, ask=4159.20),
                SymbolInfo(name=symbol, digits=2, point=0.01, volume_min=0.01, volume_step=0.01, raw={"trade_tick_size": 0.01}),
            )

        async def place_pending_order(self, body):
            self.sent.append(dict(body))
            return {"ok": True, "retcode": 10008, "result": {"retcode": 10008}}

    old_dry_run = config.dry_run
    old_execution_mode = config.execution_mode
    old_timeframe = config.timeframe
    object.__setattr__(config, "dry_run", False)
    object.__setattr__(config, "execution_mode", "pending_limit")
    object.__setattr__(config, "timeframe", "M1")
    try:
        api = Api()
        engine = ExecutionEngine(api, Ledger(tmp_path / "reprice.sqlite3"))
        decision = Decision(status="SELL", allocation=-0.5, confidence=0.9, stop_loss=4162.6, take_profit=4155.8)
        risk = RiskResult(approved=True, reason="approved", volume=0.03, entry_price=4159.43)
        stale_tick = Tick(bid=4159.00, ask=4159.01)
        stale_info = SymbolInfo(name="XAUUSD", digits=2, point=0.01, volume_min=0.01, volume_step=0.01)
        result = asyncio.run(engine.execute("XAUUSD", 202606191646, "levels_strategy", decision, risk, stale_tick, stale_info))
    finally:
        object.__setattr__(config, "dry_run", old_dry_run)
        object.__setattr__(config, "execution_mode", old_execution_mode)
        object.__setattr__(config, "timeframe", old_timeframe)

    assert result.ok
    assert api.sent
    assert api.sent[0]["price"] > 4159.20
    assert "comment" not in api.sent[0]
