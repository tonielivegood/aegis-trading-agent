"""Rug-vector gate for copy_trade real-money buys.

Separate from the shared `passes_safety_check` (execution/binance_web3.py),
which also serves Aegis's established 147-token universe
(src/agent/data/eligible_tokens.json). A hard block on GoPlus's mint/ownership/
proxy flags would be wrong there: Aegis's own ETH holding
(0x2170ed0880ac9a755fd29b2688956bd959f933f8) trips is_mintable=1 for legitimate
Binance-bridge custody reasons (verified live 2026-07-29). This module only
gates copy_trade's own new-memecoin buy path — see
docs/superpowers/specs/2026-07-29-copy-trade-rug-check-design.md."""
from __future__ import annotations

import requests

from .net_timeout import call_with_hard_timeout
from ..monitor.logger import get_logger

log = get_logger(__name__)

_GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses="

# Verified live against PIG (0x40d46ecd7a40bef5311077aab840dcd23a123564) and
# Aegis's own ETH holding 2026-07-29 — GoPlus reports each of these reliably as
# the string "0"/"1", even for a 1-day-old contract. Deliberately excludes:
#   - is_honeypot: already covered by passes_safety_check, different source
#   - creator_percent/owner_percent: already covered by phase2_score's whale_risk
#   - LP-lock fields: null for most brand-new tokens, would false-block
_RUG_FLAGS = {
    "is_mintable": "mintable",
    "can_take_back_ownership": "fake_renounce",
    "hidden_owner": "hidden_owner",
    "owner_change_balance": "owner_can_edit_balance",
    "transfer_pausable": "transfer_pausable",
    "slippage_modifiable": "slippage_modifiable",
    "is_proxy": "upgradeable_proxy",
}


def passes_rug_check(token_address: str) -> tuple[bool, str]:
    """(True, "") if none of the GoPlus mint/ownership/proxy rug flags are set.
    Fails closed: any network error, missing record, or incomplete record
    (a flag GoPlus hasn't analysed yet) blocks the buy."""
    try:
        r = call_with_hard_timeout(requests.get, _GOPLUS + token_address,
                                   timeout=15, hard_timeout=25)
        r.raise_for_status()
        result = r.json().get("result") or {}
        info = result.get(token_address.lower()) or result.get(token_address)
        if not info:
            return False, "no_goplus_record"
        # GoPlus analyses tokens asynchronously and omits these fields entirely
        # for non-open-source contracts, so a record can exist while the flags
        # we gate on are absent. Defaulting a missing flag to "clean" would wave
        # through exactly the freshly-launched contracts this gate exists to
        # catch — treat an incomplete record as unanalysable, not as safe.
        if any(str(info.get(f, "")).strip() == "" for f in _RUG_FLAGS):
            return False, "incomplete_goplus_record"
        for field, reason in _RUG_FLAGS.items():
            if str(info[field]) == "1":
                return False, reason
        return True, ""
    except Exception as e:  # noqa: BLE001 — fail closed: no data = no buy
        log.warning("rug_check_failed", token=token_address, error=type(e).__name__)
        return False, "goplus_error"
