"""TDD for the v2 sniper orchestrator (breakout + regime valve + cooldown + exits)."""
from src.agent.aegis import sniper
from src.agent.aegis.cooldown import CooldownBook
from src.agent.aegis.positions import OpenPosition, PositionBook
from src.agent.aegis.regime import Regime
from src.agent.aegis.volume_anomaly_detector import MarketSnapshot
from src.agent.strategy.base_strategy import PortfolioState


class FakeFeed:
    """Returns pre-set snapshots; ignores live prices."""
    def __init__(self, snaps):
        self._snaps = snaps

    def build_snapshots(self, symbols, prices=None):
        return {s: self._snaps[s] for s in symbols if s in self._snaps}


def _snap(symbol, *, vol_5m, baseline_vol, price_now, price_5m_ago,
          recent_pump_pct=0.0, slippage_est=0.01, liquidity_ok=True):
    return MarketSnapshot(symbol=symbol, contract="0x" + symbol.lower(), vol_5m=vol_5m,
                          baseline_vol=baseline_vol, price_now=price_now, price_5m_ago=price_5m_ago,
                          recent_pump_pct=recent_pump_pct, slippage_est=slippage_est,
                          has_route=True, liquidity_ok=liquidity_ok)


def _state(equity=30.0, stable=30.0, risk=0.0, holdings=None, tripped=False):
    return PortfolioState(equity_usd=equity, risk_value_usd=risk, stable_value_usd=stable,
                          token_values_usd=holdings or {}, drawdown_tripped=tripped)


def _allow(_c):
    return True


def test_breakout_opens_regime_sized_entry():
    # ETH = MAJOR → needs the rare 5x vol + a confirmed +3% move; sized at regime % of NAV.
    snaps = {"ETH": _snap("ETH", vol_5m=600, baseline_vol=100, price_now=1.05, price_5m_ago=1.0)}
    book = PositionBook()
    prices = {"ETH": 1.05}
    orders, mode = sniper.run(_state(), prices, book=book, feed=FakeFeed(snaps),
                              cooldowns=CooldownBook(), regime_flag=Regime.RISK_ON,
                              universe=["ETH"], now=1000.0, floor_usd=6.0, allow=_allow)
    assert mode == "sniper"
    assert len(orders) == 1 and orders[0].token_out == "ETH"
    assert orders[0].amount_in_usd == 10.5         # 35% of $30 NAV (RISK_ON, concentrated)
    assert book.is_open("ETH")


def test_risk_on_no_longer_loosens_entry_bar():
    # The beta-capture valve was REMOVED (it over-fired live → churn bleed). A MAJOR
    # 2x-vol move is BELOW the 2.5x bar in EVERY regime → no entry, even in RISK_ON
    # (regime risks more via SIZE/SLOTS, not a looser signal bar).
    weak = {"ETH": _snap("ETH", vol_5m=200, baseline_vol=100, price_now=1.05, price_5m_ago=1.0)}
    on, _ = sniper.run(_state(), {"ETH": 1.05}, book=PositionBook(), feed=FakeFeed(weak),
                       cooldowns=CooldownBook(), regime_flag=Regime.RISK_ON,
                       universe=["ETH"], now=1000.0, floor_usd=6.0, allow=_allow)
    assert on == []
    # A genuine 6x breakout with a confirmed +5% move clears the bar and enters in RISK_ON.
    strong = {"ETH": _snap("ETH", vol_5m=600, baseline_vol=100, price_now=1.05, price_5m_ago=1.0)}
    on2, _ = sniper.run(_state(), {"ETH": 1.05}, book=PositionBook(), feed=FakeFeed(strong),
                        cooldowns=CooldownBook(), regime_flag=Regime.RISK_ON,
                        universe=["ETH"], now=1000.0, floor_usd=6.0, allow=_allow)
    assert len(on2) == 1 and on2[0].token_out == "ETH"


def test_cautious_uses_smaller_size():
    snaps = {"ETH": _snap("ETH", vol_5m=600, baseline_vol=100, price_now=1.05, price_5m_ago=1.0)}
    orders, _ = sniper.run(_state(), {"ETH": 1.05}, book=PositionBook(), feed=FakeFeed(snaps),
                           cooldowns=CooldownBook(), regime_flag=Regime.CAUTIOUS,
                           universe=["ETH"], now=1000.0, floor_usd=4.0, allow=_allow)
    assert orders[0].amount_in_usd == 6.0          # 20% of $30 (CAUTIOUS, MAJOR sized)


def test_risk_off_blocks_all_entries():
    snaps = {"AAA": _snap("AAA", vol_5m=400, baseline_vol=100, price_now=1.05, price_5m_ago=1.0)}
    orders, _ = sniper.run(_state(), {"AAA": 1.05}, book=PositionBook(), feed=FakeFeed(snaps),
                           cooldowns=CooldownBook(), regime_flag=Regime.RISK_OFF,
                           universe=["AAA"], now=1000.0, allow=_allow)
    assert orders == []


def test_cooldown_blocks_reentry():
    snaps = {"AAA": _snap("AAA", vol_5m=400, baseline_vol=100, price_now=1.05, price_5m_ago=1.0)}
    cb = CooldownBook()
    cb.record_exit("AAA", now=1000.0)
    orders, _ = sniper.run(_state(), {"AAA": 1.05}, book=PositionBook(), feed=FakeFeed(snaps),
                           cooldowns=cb, regime_flag=Regime.RISK_ON, universe=["AAA"],
                           now=1000.0 + 600, cooldown_s=5400, floor_usd=6.0, allow=_allow)
    assert orders == []


def test_exit_records_cooldown_and_frees_slot():
    # A held position at -15% should hard-stop out and enter cooldown (below both tiers' stops).
    book = PositionBook()
    book.open(OpenPosition(symbol="OLD", contract="0xold", entry_price=1.0, usd_size=6.0,
                           entry_time=0.0))
    snaps = {"OLD": _snap("OLD", vol_5m=0, baseline_vol=0, price_now=0.85, price_5m_ago=0.95)}
    cb = CooldownBook()
    orders, _ = sniper.run(_state(holdings={"OLD": 5.1}), {"OLD": 0.85}, book=book,
                           feed=FakeFeed(snaps), cooldowns=cb, regime_flag=Regime.RISK_ON,
                           universe=["OLD"], now=1000.0, allow=_allow)
    assert any(o.token_in == "OLD" and o.token_out == "USDT" for o in orders)
    assert not book.is_open("OLD")
    assert "OLD" in cb.cooling_down(now=1000.0, cooldown_s=5400)


def test_manage_classes_leaves_other_sleeves_positions_alone():
    # Barbell: when the sniper owns MEMES only, it must NOT exit a MAJOR position (the beta
    # core owns those) even when that major is deep underwater.
    snaps = {"ETH": _snap("ETH", vol_5m=0, baseline_vol=0, price_now=0.85, price_5m_ago=0.95)}

    def _book_with_major():
        b = PositionBook()
        b.open(OpenPosition(symbol="ETH", contract="0xeth", entry_price=1.0, usd_size=10.0,
                            token_class="major"))
        return b

    held = _book_with_major()
    orders, _ = sniper.run(_state(holdings={"ETH": 8.5}), {"ETH": 0.85}, book=held,
                           feed=FakeFeed(snaps), cooldowns=CooldownBook(), regime_flag=Regime.RISK_ON,
                           universe=["ETH"], now=1000.0, allow=_allow, manage_classes={"meme"})
    assert orders == [] and held.is_open("ETH")          # meme sleeve doesn't touch the major

    control = _book_with_major()
    orders2, _ = sniper.run(_state(holdings={"ETH": 8.5}), {"ETH": 0.85}, book=control,
                            feed=FakeFeed(snaps), cooldowns=CooldownBook(), regime_flag=Regime.RISK_ON,
                            universe=["ETH"], now=1000.0, allow=_allow)
    assert any(o.token_in == "ETH" for o in orders2) and not control.is_open("ETH")  # default manages it


def test_max_meme_positions_caps_global_exposure():
    # Two clean meme breakouts, but the GLOBAL cap (max_meme_positions) allows only 1.
    snaps = {"AAA": _snap("AAA", vol_5m=600, baseline_vol=100, price_now=1.05, price_5m_ago=1.0),
             "BBB": _snap("BBB", vol_5m=600, baseline_vol=100, price_now=1.05, price_5m_ago=1.0)}
    book = PositionBook()
    orders, _ = sniper.run(_state(stable=50), {"AAA": 1.05, "BBB": 1.05}, book=book,
                           feed=FakeFeed(snaps), cooldowns=CooldownBook(), regime_flag=Regime.RISK_ON,
                           universe=["AAA", "BBB"], now=1000.0, floor_usd=6.0, allow=_allow,
                           max_meme_positions=1)
    assert len([o for o in orders if o.token_in == "USDT"]) == 1   # capped at 1, not 2
    # cap 0 → no entries at all (beta already took the global budget)
    orders0, _ = sniper.run(_state(stable=50), {"AAA": 1.05, "BBB": 1.05}, book=PositionBook(),
                            feed=FakeFeed(snaps), cooldowns=CooldownBook(), regime_flag=Regime.RISK_ON,
                            universe=["AAA", "BBB"], now=1000.0, floor_usd=6.0, allow=_allow,
                            max_meme_positions=0)
    assert orders0 == []


def test_breaker_flattens_and_blocks():
    book = PositionBook()
    book.open(OpenPosition(symbol="OLD", contract="0xold", entry_price=1.0, usd_size=6.0))
    snaps = {"AAA": _snap("AAA", vol_5m=400, baseline_vol=100, price_now=1.05, price_5m_ago=1.0)}
    cb = CooldownBook()
    orders, mode = sniper.run(_state(holdings={"OLD": 6.0}, tripped=True), {"OLD": 1.0, "AAA": 1.05},
                              book=book, feed=FakeFeed(snaps), cooldowns=cb,
                              regime_flag=Regime.RISK_ON, universe=["AAA"], now=1000.0, allow=_allow)
    assert mode == "sniper-breaker"
    assert all(o.token_out == "USDT" for o in orders)     # only flatten, no buys
    assert "OLD" in cb.cooling_down(now=1000.0, cooldown_s=5400)
