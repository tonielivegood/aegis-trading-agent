from unittest.mock import MagicMock

from src.agent.copy_trade.chain_events import ChainEventSource, WalletEvent
from src.agent.copy_trade.rpc_pool import TRANSFER_TOPIC, addr_topic

W = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
TOKEN = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
USDT = "0x55d398326f99059ff775485246999027b3197955"


def _log(token, frm, to, block=101, tx="0x" + "f" * 64):
    return {"address": token, "blockNumber": hex(block), "transactionHash": tx,
            "data": hex(10**18),
            "topics": [TRANSFER_TOPIC, addr_topic(frm), addr_topic(to)]}


def _source(logs, receipt_has_swap=True, latest=150):
    pool = MagicMock()
    pool.latest_block.return_value = latest

    def _get_logs(flt):
        # real RPC only returns logs whose block falls inside the requested
        # range — a fixed return_value would double-count across chunks.
        # A log missing blockNumber (malformed-log test fixtures) can't be
        # range-filtered — pass it through so _to_event's own handling is
        # what's under test, not this mock.
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        in_range = [lg for lg in logs
                    if "blockNumber" not in lg or lo <= int(lg["blockNumber"], 16) <= hi]
        # the bulk swap-topic query (_fetch_swap_tx_hashes) always passes
        # topics[0] as a LIST ([V2_SWAP_TOPIC, V3_SWAP_TOPIC]); the regular
        # transfer query always passes a single topic string — that shape is
        # how this mock tells the two queries apart.
        topics0 = (flt.get("topics") or [None])[0]
        if isinstance(topics0, list):
            if not receipt_has_swap:
                return []
            return [{"transactionHash": lg["transactionHash"]} for lg in in_range
                    if "transactionHash" in lg]
        return in_range
    pool.get_logs.side_effect = _get_logs
    return ChainEventSource(pool, wallets=[W], start_block=100,
                            ignore_tokens={USDT}), pool


def test_incoming_with_swap_event_is_buy():
    src, _ = _source([_log(TOKEN, OTHER, W)])
    events = src.poll()
    assert events == [WalletEvent(wallet=W, token_address=TOKEN, direction="in",
                                  amount_raw=10**18, tx_hash="0x" + "f" * 64, block=101)]


def test_incoming_without_swap_event_is_dropped():
    src, _ = _source([_log(TOKEN, OTHER, W)], receipt_has_swap=False)
    assert src.poll() == []


def test_outgoing_needs_no_swap_event():
    src, _ = _source([_log(TOKEN, W, OTHER)], receipt_has_swap=False)
    assert [e.direction for e in src.poll()] == ["out"]


def test_ignore_tokens_filtered():
    src, _ = _source([_log(USDT, OTHER, W)])
    assert src.poll() == []


def test_poll_uses_small_chunk_so_a_large_gap_never_gets_permanently_stuck():
    # Real incident, 2026-07-17: a whole poll gap sent as ONE eth_getLogs call,
    # and free RPC endpoints cap ranges at 50-250 blocks — any gap over that
    # raised before last_processed ever advanced, so the bot went permanently
    # blind after one slow tick. Small chunking absorbs any gap size instead.
    src, pool = _source([], latest=100_000)  # a huge gap from start_block=100
    src.poll()
    for call in pool.get_logs.call_args_list:
        flt = call.args[0]
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        assert hi - lo + 1 <= 40


def test_poll_advances_and_never_rescans():
    src, pool = _source([_log(TOKEN, OTHER, W)])
    src.poll()
    assert src.last_processed == 150
    pool.get_logs.side_effect = None
    pool.get_logs.return_value = []
    pool.latest_block.return_value = 160
    src.poll()
    flt = pool.get_logs.call_args_list[-1].args[0]
    assert int(flt["fromBlock"], 16) == 151


def test_poll_with_a_past_deadline_makes_no_progress_and_calls_nothing():
    # deadline already elapsed before poll() even starts scanning — must not
    # advance last_processed at all (real incident 2026-07-23: a single slow
    # poll() took 50+ minutes; a bounded tick must be able to bail this cleanly
    # and pick the same range back up next tick instead of skipping it).
    import time
    src, pool = _source([], latest=1000)
    src.poll(deadline=time.monotonic() - 1)
    assert src.last_processed == 100          # unchanged from start_block
    pool.get_logs.assert_not_called()


def test_poll_tracks_directions_independently_no_rescan_when_one_finishes_first():
    # Simulates a deadline having cut "out" short in a PRIOR poll while "in"
    # fully finished — this poll must only rescan out's remaining gap, never
    # re-request in's already-completed range. Sharing one last_processed
    # counter across both directions (the pre-fix design) would have redone
    # in's 151-200 here too, double-recording those events.
    src, pool = _source([], latest=200)
    src._last_processed["in"] = 200    # already fully caught up
    src._last_processed["out"] = 150   # still behind
    calls = []

    def _get_logs(flt):
        calls.append((int(flt["fromBlock"], 16), int(flt["toBlock"], 16)))
        return []
    pool.get_logs.side_effect = _get_logs
    src.poll()
    assert calls == [(151, 190), (191, 200)]   # only out's gap, chunked
    assert src._last_processed == {"in": 200, "out": 200}


def test_poll_gives_in_direction_at_most_half_the_budget_so_out_isnt_starved(monkeypatch):
    # Real incident 2026-07-24: "in" does more work per block (the bulk swap
    # query) and, sharing one deadline, consumed the ENTIRE tick's budget
    # every time — "out" (sell/exit signals) got zero chunks, tick after
    # tick, even with a huge backlog on both sides.
    src, pool = _source([], latest=100_000)
    calls = []

    def _get_logs(flt):
        calls.append(flt["topics"][2] is not None)   # True only for "in"
        return []
    pool.get_logs.side_effect = _get_logs

    clock = {"t": 0.0}

    def _fake_monotonic():
        clock["t"] += 1.0
        return clock["t"]
    monkeypatch.setattr("src.agent.copy_trade.chain_events.time.monotonic", _fake_monotonic)

    src.poll(deadline=22.0)
    in_count = sum(calls)
    out_count = len(calls) - in_count
    assert in_count > 0 and out_count > 0        # neither got zero
    assert out_count >= in_count * 0.5           # out isn't starved


def test_scan_chunked_stops_at_deadline_and_reports_completed_boundary(monkeypatch):
    src, pool = _source([], latest=200)   # needs 3 chunks: 101-140, 141-180, 181-200
    calls = []

    def _get_logs(flt):
        calls.append((int(flt["fromBlock"], 16), int(flt["toBlock"], 16)))
        return []
    pool.get_logs.side_effect = _get_logs

    clock = {"t": 0.0}

    def _fake_monotonic():
        clock["t"] += 1.0
        return clock["t"]
    monkeypatch.setattr("src.agent.copy_trade.chain_events.time.monotonic", _fake_monotonic)

    # deadline check happens before each chunk (t=1, t=2, t=3, ...); with
    # deadline=2.5 the first two checks pass (do chunk 1, chunk 2), the third
    # (t=3) trips — chunk 3 (181-200) never runs.
    events, reached = src._scan_chunked(101, 200, [TRANSFER_TOPIC, None, None],
                                        "in", deadline=2.5)
    assert reached == 180
    assert calls == [(101, 140), (141, 180)]


def test_no_new_blocks_returns_empty_without_scanning():
    src, pool = _source([], latest=100)   # start_block=100 == latest: nothing new
    assert src.last_processed == 100
    assert src.poll() == []


def test_wallet_to_wallet_between_tracked_emits_out_only_for_sender():
    src, _ = _source([_log(TOKEN, W, W)], receipt_has_swap=False)
    # degenerate self-transfer: must not crash, must not emit "in" without swap
    assert all(e.direction == "out" for e in src.poll())


def test_log_missing_address_is_skipped_not_crashed():
    lg = _log(TOKEN, W, OTHER)
    del lg["address"]
    src, _ = _source([lg])
    assert src.poll() == []   # malformed log skipped, poll() doesn't raise


def test_log_missing_block_number_is_skipped_not_crashed():
    lg = _log(TOKEN, W, OTHER)
    del lg["blockNumber"]
    src, _ = _source([lg])
    assert src.poll() == []   # malformed log skipped, poll() doesn't raise


def test_in_direction_uses_one_bulk_swap_query_not_one_per_transfer():
    # Real incident 2026-07-24: one 40-block chunk had 32-94 matching
    # transfers, and the old per-tx eth_getTransactionReceipt approach made
    # that many serial RPC round-trips (~0.5s each) — enough alone to eat an
    # entire tick's budget and permanently stall catch-up. The fix: exactly
    # ONE extra eth_getLogs call per chunk covers the whole chunk's swaps.
    many_logs = [_log(TOKEN, OTHER, W, block=101, tx=f"0x{i:064x}") for i in range(20)]
    # latest=140: the whole 101-140 range fits in exactly one 40-block chunk
    src, pool = _source(many_logs, latest=140)   # receipt_has_swap=True: all 20 are buys
    events = src.poll()
    assert len(events) == 20
    # calls: 1 transfer query (in) + 1 swap query (in) + 1 transfer query
    # (out, no matches, no swap query needed) = 3 total — NOT 20+ receipt calls.
    assert pool.get_logs.call_count == 3


def test_no_matching_transfers_skips_the_swap_query_entirely():
    src, pool = _source([])   # nothing matches this chunk
    src.poll()
    for call in pool.get_logs.call_args_list:
        topics0 = call.args[0]["topics"][0]
        assert not isinstance(topics0, list)   # never the swap query


def test_swap_query_ignores_entries_missing_tx_hash_without_crashing():
    # A malformed swap-log entry (no transactionHash) must not crash
    # _fetch_swap_tx_hashes; a real swap entry for the transfer's own tx
    # (the default "0x"+"f"*64 from _log()) is still found alongside it.
    src, pool = _source([_log(TOKEN, OTHER, W)])

    def _get_logs(flt):
        topics0 = (flt.get("topics") or [None])[0]
        if isinstance(topics0, list):
            return [{"topics": []}, {"transactionHash": "0x" + "f" * 64}]
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        return [lg for lg in [_log(TOKEN, OTHER, W)]
                if lo <= int(lg["blockNumber"], 16) <= hi]
    pool.get_logs.side_effect = _get_logs
    events = src.poll()
    assert [e.direction for e in events] == ["in"]
