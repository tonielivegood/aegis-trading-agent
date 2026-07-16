from scripts.build_bsc_smart_wallets import assemble_candidates, gmgn_maker_counts

W1, W2, W3 = "0x" + "1"*40, "0x" + "2"*40, "0x" + "3"*40


def test_gmgn_maker_counts():
    trades = [{"maker": W1}, {"maker": W1}, {"maker": W2}, {"maker": None}]
    assert gmgn_maker_counts(trades) == {W1: 2, W2: 1}


def test_assemble_candidates_merges_sources():
    cands = assemble_candidates(gmgn_counts={W1: 4, W2: 1},
                                early_counts={W1: 3, W3: 2})
    by_addr = {c["address"]: c for c in cands}
    assert by_addr[W1]["sources"] == ["gmgn", "early_buyer"]
    assert by_addr[W1]["score"] > by_addr[W2]["score"]   # both-sources + wins beat gmgn-only
    assert by_addr[W3]["sources"] == ["early_buyer"]
