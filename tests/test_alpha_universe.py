"""Eligible/Alpha universe tests.

The contest scores only trades within the official 149-token BEP-20 allowlist,
matched by CONTRACT ADDRESS. The token registry must therefore:
  - serve the tradable Alpha tokens (so pricing/execution stop KeyError-ing on
    anything outside the majors),
  - resolve tokens by address,
  - treat eligibility STRICTLY as membership in the official allowlist (no
    "majors are always eligible" assumption — that was the root bug).

These tests touch no network. Pricing parity is checked by mocking the router.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent.data import price_feed, token_list

_ALPHA = Path("src/agent/data/tradable_alpha.json")
_ELIGIBLE = Path("src/agent/data/eligible_tokens.json")


def _first_alpha_symbol() -> str:
    syms = token_list.alpha_symbols()
    if not syms:
        pytest.skip("tradable_alpha.json not built yet")
    return syms[0]


def test_alpha_universe_loads_and_is_nonempty():
    syms = token_list.alpha_symbols()
    if not _ALPHA.exists():
        pytest.skip("tradable_alpha.json not built")
    assert syms, "expected a non-empty tradable Alpha universe"


def test_get_token_resolves_alpha_token_not_in_core():
    sym = _first_alpha_symbol()
    tok = token_list.get_token(sym)            # must NOT raise KeyError
    assert tok.address.startswith("0x") and len(tok.address) == 42
    assert tok.decimals >= 0


def test_get_token_by_address_round_trips():
    sym = _first_alpha_symbol()
    tok = token_list.get_token(sym)
    found = token_list.get_token_by_address(tok.contract.lower())
    assert found is not None and found.contract.lower() == tok.contract.lower()


def test_get_token_by_address_unknown_is_none():
    assert token_list.get_token_by_address("0x000000000000000000000000000000000000dead") is None


def test_eligibility_is_strict_to_official_allowlist():
    if not _ELIGIBLE.exists():
        pytest.skip("eligible_tokens.json missing")
    elig = json.loads(_ELIGIBLE.read_text(encoding="utf-8"))
    a_real = next(t["contract"] for t in elig if t.get("contract"))
    assert token_list.is_eligible(a_real)
    assert not token_list.is_eligible("0x000000000000000000000000000000000000dead")


def test_unknown_token_still_raises():
    with pytest.raises(KeyError):
        token_list.get_token("NOTAREALTOKENXYZ")


def test_every_tradable_alpha_token_is_eligible_by_contract():
    # Anti-DQ guard: the agent's trade universe must be a subset of the official
    # allowlist (matched by contract address). A token outside it would score 0.
    if not token_list.alpha_symbols():
        pytest.skip("tradable_alpha.json not built")
    for tok in token_list.tradable_alpha_tokens():
        assert token_list.is_eligible(tok.contract), f"{tok.symbol} not in official allowlist"


def test_pricing_works_for_alpha_token_via_router(mocker):
    # Proves the KeyError blocker is gone: price_feed can value an Alpha token
    # once get_token serves it. Router is mocked — no network.
    sym = _first_alpha_symbol()
    tok = token_list.get_token(sym)
    fake = mocker.Mock()
    # 1 token -> some USDT out (18 decimals on BSC)
    fake.functions.getAmountsOut.return_value.call.return_value = [10 ** tok.decimals, 3 * 10 ** 18]
    mocker.patch("src.agent.data.price_feed._router", return_value=fake)
    price = price_feed.onchain_price_usd(sym)
    assert price == pytest.approx(3.0)
