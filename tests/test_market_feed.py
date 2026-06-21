"""MarketFeed tests — rolling-cache math and snapshot assembly. No network:
on-chain price + slippage are mocked; volume comes from the injected provider.
"""
from __future__ import annotations

from src.agent.aegis.market_feed import MarketFeed, _price_5m_and_min


def test_price_5m_and_min_picks_old_price_and_recent_min():
    now = 10_000.0
    samples = [(now - 600, 1.0), (now - 400, 0.9), (now - 100, 1.2)]
    p5, lo = _price_5m_and_min(samples, now)
    assert p5 == 0.9      # most-recent sample still >= 5 min old
    assert lo == 0.9      # recent minimum


def test_price_5m_and_min_empty():
    assert _price_5m_and_min([], 1.0) == (0.0, 0.0)


def test_snapshot_no_route_when_price_missing(mocker, tmp_path):
    mocker.patch("src.agent.aegis.market_feed.price_feed.onchain_price_usd", return_value=None)
    feed = MarketFeed(order_usd=10, cache_path=tmp_path / "c.json")
    snap = feed.snapshot("TWT")
    assert snap.has_route is False and snap.liquidity_ok is False


def test_snapshot_builds_with_price_slippage_and_volume(mocker, tmp_path):
    feed = MarketFeed(order_usd=10, max_slippage=0.05, cache_path=tmp_path / "c.json",
                      volume_provider=lambda s: (500.0, 100.0))
    mocker.patch("src.agent.aegis.market_feed.token_list.tradable_slippage", return_value=0.01)
    snap = feed.snapshot("TWT", price=1.0)
    assert snap.has_route and snap.liquidity_ok           # 1% slippage <= 5% max
    assert snap.price_now == 1.0
    assert snap.vol_5m == 500.0 and snap.baseline_vol == 100.0


def test_snapshot_uses_3tuple_provider_move_as_breakout(mocker, tmp_path):
    # A 3-tuple provider (vol, baseline, move) sets the snapshot's authoritative move.
    feed = MarketFeed(order_usd=10, max_slippage=0.05, cache_path=tmp_path / "c.json",
                      volume_provider=lambda s: (500.0, 100.0, 0.08))
    mocker.patch("src.agent.aegis.market_feed.token_list.tradable_slippage", return_value=0.01)
    snap = feed.snapshot("TWT", price=1.0)
    assert snap.vol_5m == 500.0 and snap.baseline_vol == 100.0
    assert snap.breakout_pct == 0.08


def test_snapshot_2tuple_provider_leaves_move_none(mocker, tmp_path):
    # Legacy 2-tuple provider → breakout_pct stays None (scan falls back to the cache).
    feed = MarketFeed(order_usd=10, max_slippage=0.05, cache_path=tmp_path / "c.json",
                      volume_provider=lambda s: (500.0, 100.0))
    mocker.patch("src.agent.aegis.market_feed.token_list.tradable_slippage", return_value=0.01)
    snap = feed.snapshot("TWT", price=1.0)
    assert snap.breakout_pct is None


def test_snapshot_marks_illiquid_when_slippage_exceeds_max(mocker, tmp_path):
    feed = MarketFeed(order_usd=10, max_slippage=0.05, cache_path=tmp_path / "c.json")
    mocker.patch("src.agent.aegis.market_feed.token_list.tradable_slippage", return_value=0.20)
    snap = feed.snapshot("TWT", price=1.0)
    assert snap.has_route and snap.liquidity_ok is False


def test_meme_uses_looser_slippage_gate_than_major(mocker, tmp_path):
    # 5% slippage: FAILS the 4% major gate but PASSES the 6% meme gate (small lottery size).
    feed = MarketFeed(order_usd=10, max_slippage=0.04, cache_path=tmp_path / "c.json")
    mocker.patch("src.agent.aegis.market_feed.token_list.tradable_slippage", return_value=0.05)
    tc = mocker.patch("src.agent.aegis.market_feed.token_list.token_class", return_value="meme")
    assert feed.snapshot("ETH", price=1.0).liquidity_ok is True       # meme → 6% gate
    tc.return_value = "major"
    assert feed.snapshot("ETH", price=1.0).liquidity_ok is False      # major → 4% gate


def test_cache_persists_across_instances(mocker, tmp_path):
    p = tmp_path / "c.json"
    f1 = MarketFeed(order_usd=10, max_slippage=0.5, cache_path=p)
    mocker.patch.object(f1, "_estimate_slippage", return_value=0.0)
    f1.snapshot("TWT", price=1.0)
    f1.save()
    f2 = MarketFeed(order_usd=10, cache_path=p)
    assert "TWT" in f2.cache and f2.cache["TWT"]
