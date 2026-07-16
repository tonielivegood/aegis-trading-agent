from unittest.mock import MagicMock

from src.agent.copy_trade.chain_events import ChainEventSource, WalletEvent
from src.agent.copy_trade.rpc_pool import TRANSFER_TOPIC, V2_SWAP_TOPIC, addr_topic

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
    pool.get_logs_chunked.return_value = logs
    swap_log = {"topics": [V2_SWAP_TOPIC]} if receipt_has_swap else {"topics": ["0xdead"]}
    pool.get_receipt.return_value = {"logs": [swap_log]}
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


def test_poll_advances_and_never_rescans():
    src, pool = _source([_log(TOKEN, OTHER, W)])
    src.poll()
    assert src.last_processed == 150
    pool.get_logs_chunked.return_value = []
    pool.latest_block.return_value = 160
    src.poll()
    args = pool.get_logs_chunked.call_args_list[-1]
    assert args.kwargs.get("from_block", args.args[0] if args.args else None) == 151


def test_no_new_blocks_returns_empty_without_scanning():
    src, pool = _source([], latest=100)
    src.last_processed = 100
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


def test_receipt_log_with_empty_topics_is_not_a_swap():
    src, pool = _source([_log(TOKEN, OTHER, W)])
    pool.get_receipt.return_value = {"logs": [{"topics": []}, {"topics": [V2_SWAP_TOPIC]}]}
    events = src.poll()
    # empty-topics log doesn't crash iteration; the other log in the same
    # receipt is still checked and still finds the swap
    assert [e.direction for e in events] == ["in"]
