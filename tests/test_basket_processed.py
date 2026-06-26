from core.ledger import Ledger


def test_mark_basket_processed(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    ledger = Ledger(db)
    assert not ledger.is_basket_processed("EURUSD", "M15", 202606171600, "levels_strategy")
    ledger.mark_basket_processed(
        symbol="EURUSD",
        timeframe="M15",
        uid=202606171600,
        strategy="levels_strategy",
        data={"broker_now": "2026-06-17T16:07:00+00:00"},
    )
    assert ledger.is_basket_processed("EURUSD", "M15", 202606171600, "levels_strategy")


