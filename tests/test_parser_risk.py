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
