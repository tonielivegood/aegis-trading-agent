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
