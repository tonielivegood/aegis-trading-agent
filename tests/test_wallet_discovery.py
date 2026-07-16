from src.agent.copy_trade.wallet_discovery import (
    cross_winner_candidates, early_buyers,
)
from src.agent.copy_trade.rpc_pool import TRANSFER_TOPIC, addr_topic

W1 = "0x1111111111111111111111111111111111111111"
W2 = "0x2222222222222222222222222222222222222222"
W3 = "0x3333333333333333333333333333333333333333"
PAIR = "0x9999999999999999999999999999999999999999"
ZERO = "0x0000000000000000000000000000000000000000"


def _transfer_log(to_addr: str, block: int) -> dict:
    return {"address": "0xtoken", "blockNumber": hex(block),
            "topics": [TRANSFER_TOPIC, addr_topic(PAIR), addr_topic(to_addr)]}


def test_early_buyers_first_seen_order_dedup_and_exclusions():
    logs = [
        _transfer_log(ZERO, 1),          # excluded (zero)
        _transfer_log(W1, 2),
        _transfer_log(PAIR, 3),          # excluded (the pair itself)
        _transfer_log(W2, 4),
        _transfer_log(W1, 5),            # duplicate
    ]
    assert early_buyers(logs, exclude={PAIR, ZERO}) == [W1, W2]


def test_early_buyers_caps_at_max():
    logs = [_transfer_log(f"0x{i:040x}", i) for i in range(1, 50)]
    assert len(early_buyers(logs, exclude=set(), max_buyers=10)) == 10


def test_cross_winner_candidates_requires_min_tokens():
    buyers = {"tokA": [W1, W2], "tokB": [W1, W3], "tokC": [W1]}
    cands = cross_winner_candidates(buyers, min_tokens=2)
    assert cands == {W1: 3}          # W1 early in 3 winners; W2/W3 only in 1 each


from src.agent.copy_trade.wallet_discovery import (
    build_ranked_list, passes_filters, score_candidate, wallet_activity,
)


def test_passes_filters_rejects_contract_bot_cold_and_inactive():
    ok_act = {"tx_7d": 20, "tx_per_day": 10.0, "last_tx_age_days": 1.0}
    assert passes_filters(ok_act, code="0x")[0] is True
    assert passes_filters(ok_act, code="0x6080")[0] is False          # contract
    assert passes_filters({**ok_act, "tx_per_day": 500.0}, "0x")[0] is False  # bot
    assert passes_filters({**ok_act, "last_tx_age_days": 9.0}, "0x")[0] is False  # cold
    assert passes_filters({**ok_act, "tx_7d": 1}, "0x")[0] is False   # barely trades


def test_score_candidate_weights():
    assert score_candidate(wins_early=3, gmgn_hits=0, in_both=False) == 6.0
    assert score_candidate(wins_early=0, gmgn_hits=10, in_both=False) == 3.0
    assert score_candidate(wins_early=2, gmgn_hits=5, in_both=True) == 2*2.0 + 1.5 + 3.0
    assert score_candidate(wins_early=99, gmgn_hits=99, in_both=True) == 10.0 + 3.0 + 3.0


def test_build_ranked_list_sorts_and_truncates():
    cands = [{"address": f"0x{i:040x}", "score": float(i)} for i in range(60)]
    ranked = build_ranked_list(cands, top_n=50)
    assert len(ranked) == 50
    assert ranked[0]["score"] == 59.0


def test_wallet_activity_parses_txlist(monkeypatch):
    now = 1_000_000_000
    txs = [{"timeStamp": str(now - 3600)}, {"timeStamp": str(now - 86400 * 2)},
           {"timeStamp": str(now - 86400 * 10)}]
    class FakeResp:
        status_code = 200
        def json(self):
            return {"status": "1", "result": txs}
        def raise_for_status(self):
            pass
    monkeypatch.setattr("src.agent.copy_trade.wallet_discovery.requests.get",
                        lambda url, params=None, timeout=None: FakeResp())
    act = wallet_activity("KEY", "0xabc", now_ts=now)
    assert act["tx_7d"] == 2
    assert act["last_tx_age_days"] < 1.0


def test_wallet_activity_returns_none_on_api_failure(monkeypatch):
    def boom(url, params=None, timeout=None):
        raise ConnectionError("down")
    monkeypatch.setattr("src.agent.copy_trade.wallet_discovery.requests.get", boom)
    assert wallet_activity("KEY", "0xabc", now_ts=0) is None
