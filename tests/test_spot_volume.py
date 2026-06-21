"""TDD for the Binance spot klines volume provider (MAJORS volume source)."""
import time

from src.agent.aegis.binance_spot_volume import BinanceSpotKlinesVolumeProvider


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    def __init__(self, payload):
        self._p = payload
        self.last = None

    def get(self, url, params=None, timeout=None):
        self.last = (url, params)
        return _Resp(self._p)


def _kline(open_time, close, quote_vol, close_time):
    # [openTime, open, high, low, close, baseVol, closeTime, quoteVol, nTrades, ...]
    return [open_time, "1.0", "1.1", "0.9", str(close), "10", close_time, str(quote_vol), 5, "0", "0", "0"]


def _rows(baseline_qv, current_qv, current_close, *, fresh=True):
    now_ms = int(time.time() * 1000)
    rows = []
    for i in range(24):
        ct = now_ms - (24 - i) * 60_000
        rows.append(_kline(ct - 60_000, 1.0, baseline_qv, ct))
    cur_ct = now_ms if fresh else now_ms - 3_600_000
    rows.append(_kline(now_ms - 60_000, current_close, current_qv, cur_ct))
    return rows


def test_spot_volume_spike_detected():
    sess = _Session(_rows(baseline_qv=100, current_qv=300, current_close=1.05))
    p = BinanceSpotKlinesVolumeProvider(session=sess, freshness_s=600)
    v = p.get("CAKE")
    assert v.available
    assert v.alpha_symbol == "CAKEUSDT"
    assert round(v.volume_multiple, 2) == 3.0
    assert v.price_change_5m_pct > 0
    assert "CAKEUSDT" in sess.last[1]["symbol"]


def test_volume_tuple_shape():
    sess = _Session(_rows(100, 250, 1.02))
    p = BinanceSpotKlinesVolumeProvider(session=sess)
    cur, base = p.volume_tuple("ETH")
    assert cur == 250 and base == 100


def test_volume_and_move_returns_same_source_move():
    # current close 1.05 vs prior 1.0 => +5% move, from the SAME klines as the volume.
    sess = _Session(_rows(baseline_qv=100, current_qv=300, current_close=1.05))
    p = BinanceSpotKlinesVolumeProvider(session=sess, freshness_s=600)
    cur, base, move = p.volume_and_move("CAKE")
    assert cur == 300 and base == 100
    assert move is not None and abs(move - 0.05) < 1e-9


def test_volume_and_move_unavailable_returns_none_move():
    sess = _Session(_rows(100, 300, 1.05, fresh=False))   # stale → unavailable
    p = BinanceSpotKlinesVolumeProvider(session=sess, freshness_s=600)
    assert p.volume_and_move("CAKE") == (0.0, 0.0, None)


def test_allowlist_blocks_non_major():
    sess = _Session(_rows(100, 300, 1.05))
    p = BinanceSpotKlinesVolumeProvider(session=sess, symbols={"CAKE", "ETH"})
    assert p.volume_tuple("DOGE") == (0.0, 0.0)     # not in the major allowlist


def test_stale_candle_failsafe():
    sess = _Session(_rows(100, 300, 1.05, fresh=False))
    p = BinanceSpotKlinesVolumeProvider(session=sess, freshness_s=600)
    assert p.volume_tuple("CAKE") == (0.0, 0.0)


def test_quote_symbol_itself_unavailable():
    p = BinanceSpotKlinesVolumeProvider(session=_Session([]))
    assert p.volume_tuple("USDT") == (0.0, 0.0)
