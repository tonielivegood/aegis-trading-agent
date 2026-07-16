from scripts.shadow_report import summarize


def _row(pnl_usd, pnl_pct, fees=0.5, simulated=True):
    return {"simulated": simulated, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
            "fees_model_usd": fees}


def test_summarize_stats():
    rows = [_row(1.0, 0.5), _row(-0.5, -0.2), _row(2.0, 1.0)]
    s = summarize(rows)
    assert s["closed"] == 3 and s["wins"] == 2
    assert abs(s["win_rate"] - 2 / 3) < 1e-9
    assert s["total_pnl_usd"] == 2.5
    assert s["median_pnl_pct"] == 0.5


def test_summarize_only_counts_simulated():
    rows = [_row(1.0, 0.5), _row(9.0, 9.0, simulated=False)]
    assert summarize(rows)["closed"] == 1


def test_summarize_empty():
    s = summarize([])
    assert s["closed"] == 0 and s["win_rate"] is None
