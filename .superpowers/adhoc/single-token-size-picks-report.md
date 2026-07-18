# Single-token size-pick candidates — report

## What was built

Added a third, additive candidate source to the BSC smart-wallet mining
pipeline: for each winner token individually, the wallets that bought the
LARGEST amount of that token in the early-buyer window (Transfer log `data`
field), independent of cross-token convergence. Existing `min_tokens=2`
convergence signal (`cross_winner_candidates`) is unchanged.

### `src/agent/copy_trade/wallet_discovery.py`

Two new pure functions, added exactly as specified:

```python
def early_buyer_amounts(logs: list[dict], exclude: set[str],
                        max_addrs: int = 200) -> dict[str, int]: ...

def top_by_amount(amounts: dict[str, int], top_n: int) -> list[str]: ...
```

- `early_buyer_amounts`: same dedup/exclude/address-decoding as `early_buyers`
  (via `_topic_to_addr`), but sums `int(lg["data"], 16)` per address across ALL
  occurrences. Caps at `max_addrs` DISTINCT addresses (new addresses stop being
  admitted once the cap is hit; already-admitted addresses keep summing).
  Malformed/missing `data` is parsed via `int(lg.get("data") or "0x0", 16)`
  wrapped in `try/except (TypeError, ValueError)` — treated as amount 0 for
  that single log entry, no exception propagates.
- `top_by_amount`: `sorted(amounts, key=lambda a: amounts[a], reverse=True)[:top_n]`.
- `early_buyers()` itself was NOT modified — new function duplicates the
  log-iteration logic as instructed (intentional, avoids a shared-logic
  refactor for two independently-testable functions).

`score_candidate` extended:

```python
def score_candidate(wins_early: int, gmgn_hits: int, in_both: bool,
                    is_size_pick: bool = False) -> float:
    return (min(wins_early, 5) * 2.0
            + min(gmgn_hits, 10) * 0.3
            + (3.0 if in_both else 0.0)
            + (1.5 if is_size_pick else 0.0))
```

Default `False` preserves every existing call site/test unchanged.

### `scripts/build_bsc_smart_wallets.py`

- `scan_winner()` now returns `tuple[list[str], dict[str, int]]` — `(buyers,
  amounts)` computed from the SAME fetched `logs` (no second RPC call). The
  exception fallback returns `([], {})`. The existing print line is untouched.
- `assemble_candidates(gmgn_counts, early_counts, size_picks: set[str] =
  frozenset())` — candidate set is now the union of all three sources.
  `sources` gains `"size_pick"` when applicable, and `is_size_pick=addr in
  size_picks` is passed to `score_candidate`.
  - **One deliberate deviation from a literal reading of the spec**: `in_both`
    is now computed as `"gmgn" in sources and "early_buyer" in sources`
    instead of the old `len(sources) == 2`. With a third possible source,
    `len(sources) == 2` would incorrectly fire "in_both" bonus for an
    early_buyer+size_pick candidate (2 sources, but not gmgn+early_buyer
    convergence). This is required for correctness of the pre-existing
    `in_both` semantics ("showing up in both independent sources" = gmgn AND
    early_buyer specifically) — confirmed via new test
    `test_assemble_candidates_early_and_size_pick_combined`, which pins the
    score to reflect early_buyer + size_pick only (no in_both bonus).
- `main()`: added `amounts_by_token`, populated alongside `buyers_by_token` by
  unpacking `scan_winner()`'s tuple. `early_counts` computation is unchanged.
  New: `size_picks` built by unioning `top_by_amount(amounts, top_n=args.
  size_picks_per_token)` across all tokens, printed as `f"  {len(size_picks)}
  additional single-token size-pick candidates"` right after the existing
  `early_counts` print line. `size_picks` passed through to
  `assemble_candidates`. Everything downstream (contract check, activity
  filtering, `build_ranked_list`, table print, file write) is untouched.
- New CLI arg: `--size-picks-per-token` (`type=int, default=10`).
- Module docstring updated with a third bullet describing the new source.

## Tests

All 11 new tests specified were written first (confirmed failing via
`ImportError: cannot import name 'early_buyer_amounts'` before
implementation), then passed after implementing:

```
tests/test_wallet_discovery.py::test_early_buyer_amounts_sums_multiple_transfers_to_same_address PASSED
tests/test_wallet_discovery.py::test_early_buyer_amounts_respects_exclude PASSED
tests/test_wallet_discovery.py::test_early_buyer_amounts_caps_distinct_addrs_but_keeps_summing_admitted PASSED
tests/test_wallet_discovery.py::test_early_buyer_amounts_treats_missing_or_malformed_data_as_zero PASSED
tests/test_wallet_discovery.py::test_top_by_amount_sorts_descending PASSED
tests/test_wallet_discovery.py::test_top_by_amount_top_n_larger_than_dict_returns_everything PASSED
tests/test_wallet_discovery.py::test_score_candidate_size_pick_bonus PASSED
tests/test_wallet_discovery.py::test_score_candidate_weights PASSED   (existing, unmodified, still passes)
tests/test_build_bsc_smart_wallets.py::test_assemble_candidates_size_pick_only PASSED
tests/test_build_bsc_smart_wallets.py::test_assemble_candidates_early_and_size_pick_combined PASSED
tests/test_build_bsc_smart_wallets.py::test_assemble_candidates_merges_sources PASSED  (existing, unmodified, still passes)
```

One incidental helper change: `_transfer_log()` in `tests/test_wallet_discovery.py`
gained an optional `data: str = "0x0"` parameter (and now always includes a
`"data"` key in the returned log dict) so the new amount tests could reuse it.
This is backward-compatible — no existing call site passes `data`, and
`early_buyers()` never reads the `data` field, so all pre-existing tests using
`_transfer_log()` are unaffected.

### Targeted run (`tests/test_wallet_discovery.py tests/test_build_bsc_smart_wallets.py -v`)

```
============================= test session starts =============================
platform win32 -- Python 3.14.4, pytest-9.1.0, pluggy-1.6.0
collected 27 items

tests/test_wallet_discovery.py::test_early_buyers_first_seen_order_dedup_and_exclusions PASSED
tests/test_wallet_discovery.py::test_early_buyers_caps_at_max PASSED
tests/test_wallet_discovery.py::test_cross_winner_candidates_requires_min_tokens PASSED
tests/test_wallet_discovery.py::test_early_buyer_amounts_sums_multiple_transfers_to_same_address PASSED
tests/test_wallet_discovery.py::test_early_buyer_amounts_respects_exclude PASSED
tests/test_wallet_discovery.py::test_early_buyer_amounts_caps_distinct_addrs_but_keeps_summing_admitted PASSED
tests/test_wallet_discovery.py::test_early_buyer_amounts_treats_missing_or_malformed_data_as_zero PASSED
tests/test_wallet_discovery.py::test_top_by_amount_sorts_descending PASSED
tests/test_wallet_discovery.py::test_top_by_amount_top_n_larger_than_dict_returns_everything PASSED
tests/test_wallet_discovery.py::test_passes_filters_rejects_contract_bot_cold_and_inactive PASSED
tests/test_wallet_discovery.py::test_score_candidate_weights PASSED
tests/test_wallet_discovery.py::test_score_candidate_size_pick_bonus PASSED
tests/test_wallet_discovery.py::test_build_ranked_list_sorts_and_truncates PASSED
tests/test_wallet_discovery.py::test_wallet_activity_parses_txlist PASSED
tests/test_wallet_discovery.py::test_wallet_activity_returns_none_on_api_failure PASSED
tests/test_build_bsc_smart_wallets.py::test_gmgn_maker_counts PASSED
tests/test_build_bsc_smart_wallets.py::test_assemble_candidates_merges_sources PASSED
tests/test_build_bsc_smart_wallets.py::test_assemble_candidates_size_pick_only PASSED
tests/test_build_bsc_smart_wallets.py::test_assemble_candidates_early_and_size_pick_combined PASSED
tests/test_build_bsc_smart_wallets.py::test_dexscreener_pair_skips_pair_missing_created_at PASSED
tests/test_build_bsc_smart_wallets.py::test_dexscreener_pair_none_when_no_pair_has_created_at PASSED
tests/test_build_bsc_smart_wallets.py::test_block_at_timestamp_finds_correct_block_without_probing_genesis PASSED
tests/test_build_bsc_smart_wallets.py::test_block_at_timestamp_raises_when_latest_block_has_no_data PASSED
tests/test_build_bsc_smart_wallets.py::test_block_at_timestamp_raises_when_a_probed_block_has_no_data PASSED
tests/test_build_bsc_smart_wallets.py::test_load_winners_file_extracts_token_addresses PASSED
tests/test_build_bsc_smart_wallets.py::test_parse_args_winners_file_and_defaults PASSED
tests/test_build_bsc_smart_wallets.py::test_parse_args_requires_some_winner_source PASSED

============================= 27 passed in 0.14s ==============================
```

### Full suite (`tests/ -q`)

```
================== 772 passed, 2 skipped, 1 warning in 5.83s ==================
```

Baseline was 763 passed, 2 skipped. 772 = 763 + 9 net new test functions
(7 in test_wallet_discovery.py, 2 in test_build_bsc_smart_wallets.py). No
regressions.

## Concerns

- None functionally blocking. The one design decision worth flagging is the
  `in_both` fix described above (`"gmgn" in sources and "early_buyer" in
  sources` instead of `len(sources) == 2`) — this is a correctness necessity
  once a third source exists, not a scope-creep change, and is covered by a
  new test.
- `scan_winner()` and `main()` wiring were not unit-tested per the task's
  explicit instruction (network-heavy / untested-orchestration convention
  already established in this file) — verified by reading the diff instead.
- Did not touch `wallets.json`, live bot trading/voting code, or anything
  execution-related, per the task's scope boundary.
