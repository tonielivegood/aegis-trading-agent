import json
from pathlib import Path

import pytest

from src.agent.copy_trade.swap_parser import parse_swap

WALLET = "0xWALLET000000000000000000000000000000001"
FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "copy_trade_swap_samples.json").read_text()
)


def test_direct_swap_parses_as_buy():
    result = parse_swap(FIXTURES["direct_swap"], WALLET)
    assert result is not None
    assert result.direction == "buy"
    assert result.token_symbol == "GEM"
    assert result.token_decimals == 9
    assert result.token_amount == pytest.approx(12345.0)
    assert result.counter_symbol == "USDT"


def test_multi_hop_swap_ignores_intermediate_wbnb_hop():
    result = parse_swap(FIXTURES["multi_hop_swap"], WALLET)
    assert result is not None
    assert result.token_symbol == "GEM2"          # not WBNB — the old bug's failure mode
    assert result.token_decimals == 9
    assert result.token_amount == pytest.approx(999.0)
    assert result.direction == "buy"


def test_ambiguous_multi_leg_tx_returns_none_instead_of_guessing():
    assert parse_swap(FIXTURES["ambiguous_multi_leg"], WALLET) is None


def test_non_swap_category_returns_none():
    assert parse_swap(FIXTURES["not_a_swap"], WALLET) is None


def test_sell_direction_when_wallet_sends_the_tracked_token():
    tx = {
        "hash": "0xeee5",
        "category": "token swap",
        "block_timestamp": "2026-07-15T10:20:00.000Z",
        "summary": "Swapped 12345 GEM for 6 USDT",
        "erc20_transfers": [
            {
                "from_address": WALLET,
                "to_address": "0xROUTER00000000000000000000000000000001",
                "token_symbol": "GEM",
                "token_decimals": "9",
                "address": "0x00000000000000000000000000000000000gem1",
                "value_formatted": "12345.0",
            },
            {
                "from_address": "0xROUTER00000000000000000000000000000001",
                "to_address": WALLET,
                "token_symbol": "USDT",
                "token_decimals": "18",
                "address": "0x55d398326f99059fF775485246999027B3197955",
                "value_formatted": "6.0",
            },
        ],
    }
    result = parse_swap(tx, WALLET)
    assert result is not None
    assert result.direction == "sell"
    assert result.token_symbol == "GEM"
    assert result.token_amount == pytest.approx(12345.0)
