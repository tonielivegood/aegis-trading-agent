from src.agent.copy_trade.wallet_discovery import (
    cross_winner_candidates, early_buyer_amounts, early_buyers, top_by_amount,
)
from src.agent.copy_trade.rpc_pool import TRANSFER_TOPIC, addr_topic

W1 = "0x1111111111111111111111111111111111111111"
W2 = "0x2222222222222222222222222222222222222222"
W3 = "0x3333333333333333333333333333333333333333"
PAIR = "0x9999999999999999999999999999999999999999"
ZERO = "0x0000000000000000000000000000000000000000"


def _transfer_log(to_addr: str, block: int, data: str = "0x0") -> dict:
    return {"address": "0xtoken", "blockNumber": hex(block),
            "topics": [TRANSFER_TOPIC, addr_topic(PAIR), addr_topic(to_addr)],
            "data": data}


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


def test_early_buyer_amounts_sums_multiple_transfers_to_same_address():
    logs = [
        _transfer_log(W1, 1, data="0x64"),    # 100
        _transfer_log(W2, 2, data="0x0a"),    # 10
        _transfer_log(W1, 3, data="0x0a"),    # +10 -> 110
    ]
    amounts = early_buyer_amounts(logs, exclude=set())
    assert amounts[W1] == 110
    assert amounts[W2] == 10


def test_early_buyer_amounts_respects_exclude():
    logs = [_transfer_log(PAIR, 1, data="0x64"), _transfer_log(ZERO, 2, data="0x64"),
            _transfer_log(W1, 3, data="0x05")]
    amounts = early_buyer_amounts(logs, exclude={PAIR, ZERO})
    assert amounts == {W1: 5}


def test_early_buyer_amounts_caps_distinct_addrs_but_keeps_summing_admitted():
    logs = [
        _transfer_log(W1, 1, data="0x0a"),          # admitted (1st distinct)
        _transfer_log(W2, 2, data="0x0a"),          # admitted (2nd distinct, cap=2)
        _transfer_log(W3, 3, data="0x0a"),          # NOT admitted (cap reached)
        _transfer_log(W1, 4, data="0x0a"),          # already-admitted, keep summing
    ]
    amounts = early_buyer_amounts(logs, exclude=set(), max_addrs=2)
    assert len(amounts) == 2
    assert amounts[W1] == 20              # both transfers counted
    assert W3 not in amounts


def test_early_buyer_amounts_treats_missing_or_malformed_data_as_zero():
    log_no_data = {"address": "0xtoken", "blockNumber": hex(1),
                   "topics": [TRANSFER_TOPIC, addr_topic(PAIR), addr_topic(W1)]}
    log_none_data = _transfer_log(W1, 2, data=None)
    log_bad_data = _transfer_log(W1, 3, data="not-hex")
    log_valid = _transfer_log(W1, 4, data="0x0a")   # 10
    amounts = early_buyer_amounts(
        [log_no_data, log_none_data, log_bad_data, log_valid], exclude=set())
    assert amounts[W1] == 10


def test_top_by_amount_sorts_descending():
    amounts = {W1: 5, W2: 50, W3: 20, PAIR: 1, ZERO: 100}
    assert top_by_amount(amounts, top_n=2) == [ZERO, W2]


def test_top_by_amount_top_n_larger_than_dict_returns_everything():
    amounts = {W1: 5, W2: 50}
    result = top_by_amount(amounts, top_n=10)
    assert set(result) == {W1, W2}
    assert len(result) == 2


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


def test_score_candidate_size_pick_bonus():
    assert score_candidate(wins_early=0, gmgn_hits=0, in_both=False,
                           is_size_pick=True) == 1.5
    # default (param omitted) must NOT silently activate the bonus
    assert score_candidate(wins_early=0, gmgn_hits=0, in_both=False) == 0.0


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
