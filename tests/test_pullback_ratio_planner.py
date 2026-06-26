from core.pullback_ratio import build_pullback_limit_plan, pullback_ratio_for_leg


def test_buy_pullback_limit_is_ratio_between_bid_and_stop():
    plan = build_pullback_limit_plan(
        side="buy",
        bid=1.10000,
        ask=1.10002,
        stop_loss=1.09900,
        ratio=0.60,
        min_gap=0.00002,
        tick_size=0.00001,
        digits=5,
    )

    assert plan is not None
    assert plan.entry_price == 1.09940


def test_sell_pullback_limit_is_ratio_between_ask_and_stop():
    plan = build_pullback_limit_plan(
        side="sell",
        bid=1.09998,
        ask=1.10000,
        stop_loss=1.10100,
        ratio=0.60,
        min_gap=0.00002,
        tick_size=0.00001,
        digits=5,
    )

    assert plan is not None
    assert plan.entry_price == 1.10060


def test_pullback_limit_respects_min_gap_when_stop_is_too_close():
    plan = build_pullback_limit_plan(
        side="buy",
        bid=1.10000,
        ask=1.10002,
        stop_loss=1.09998,
        ratio=0.60,
        min_gap=0.00005,
        tick_size=0.00001,
        digits=5,
    )

    assert plan is not None
    assert plan.entry_price == 1.09999


def test_second_split_leg_goes_deeper_toward_stop():
    assert pullback_ratio_for_leg(0.60, 1) == 0.60
    assert pullback_ratio_for_leg(0.60, 2) == 0.80


