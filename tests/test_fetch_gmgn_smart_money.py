from scripts.fetch_gmgn_smart_money import extract_wallets, merge_wallets

SAMPLE_TRADES = [
    {"maker": "0xShark0000000000000000000000000000001", "token_symbol": "GEM5", "side": "buy"},
    {"maker": "0xShark0000000000000000000000000000002", "token_symbol": "GEM6", "side": "buy"},
    {"maker": "0xShark0000000000000000000000000000001", "token_symbol": "GEM7", "side": "buy"},
]


def test_extract_wallets_deduplicates_and_labels():
    result = extract_wallets(SAMPLE_TRADES, max_wallets=10)
    addresses = [w["address"] for w in result]
    assert addresses == [
        "0xShark0000000000000000000000000000001",
        "0xShark0000000000000000000000000000002",
    ]
    assert result[0]["label"] == "GMGN_SMART_1"
    assert result[0]["monitor"] is True


def test_extract_wallets_respects_max_wallets_cap():
    many = [{"maker": f"0xW{i:039d}", "token_symbol": "X", "side": "buy"} for i in range(15)]
    result = extract_wallets(many, max_wallets=10)
    assert len(result) == 10


def test_merge_wallets_skips_addresses_already_present_case_insensitive():
    existing = [{"address": "0xABC0000000000000000000000000000000001", "label": "OLD"}]
    new = [
        {"address": "0xabc0000000000000000000000000000000001", "label": "GMGN_SMART_1"},
        {"address": "0xDEF0000000000000000000000000000000002", "label": "GMGN_SMART_2"},
    ]
    merged = merge_wallets(existing, new)
    assert len(merged) == 2
    assert merged[0]["label"] == "OLD"
    assert merged[1]["address"] == "0xDEF0000000000000000000000000000000002"
