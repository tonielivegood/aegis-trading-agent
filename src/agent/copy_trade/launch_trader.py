"""Trades the launch method measured on 4200 films: buy at 2x, sell at 6x.

The rules are not adjustable opinions — each one is the losing side of a measured
curve if changed:

- **Entry at 2x the arm price.** No token filter of any kind. Five filter
  hypotheses were tested against real data and all five died (LP-lock, GoPlus
  flags, socials, buy-volume dominance, and stop-loss-based risk management).
- **Take profit at 6x the entry.** 3x and 4x targets are NEGATIVE expectancy,
  10x is strongly negative. Only 5x-8x pays, peaking at 6x. Taking profit
  earlier feels safer and loses money.
- **No stop loss, ever.** 24 of 25 measured deaths went from healthy liquidity
  to under $1k inside ONE 30s sample. Removing liquidity is a single
  transaction; after it there is no price to sell into and no counterparty. A
  stop cannot fill, so ~66% of trades are total losses by design and the
  position size is what keeps that survivable.
- **Sell at MAX_HOLD_S regardless.** The measured expectancy closes every open
  position at the end of the 4h film. Holding longer is untested behaviour.

Sizing is the whole game: at $100/trade on a $500 bankroll, 52% of simulated
paths lose half the stack while the single historical path showed +5288%. Keep
per-trade size at 1-2% of bankroll.

Signals come from the collector's films.jsonl, which already samples every armed
token every 30s, so this adds NO API load. The collector stays a pure recorder.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings
from ..data.token_list import register_discovered
from ..execution.best_execution import rank_backends
from ..monitor.logger import get_logger
from .launch_collector import DEAD_LIQ_USD
from .prices import get_pair_stats

log = get_logger(__name__)

# parents[3], the same anchor launch_collector and monitor use. parents[4] walks
# one level ABOVE the project and silently produces a films path that does not
# exist, which read_new_film_rows treats as "nothing new yet" — the trader then
# runs forever reading zero signals and looking perfectly healthy.
ROOT = Path(__file__).resolve().parents[3]

ENTRY_MULTIPLE = 2.0
TAKE_PROFIT_MULTIPLE = 6.0
MAX_HOLD_S = 4 * 3600
# A pool this thin cannot absorb the position without moving hard against it.
# The arm floor is $20k, so this only ever rejects a pool already draining.
MIN_ENTRY_LIQ_USD = 10_000.0


@dataclass
class LaunchPosition:
    token_address: str
    symbol: str
    entry_price: float
    usd_size: float
    token_amount: float
    opened_at: float
    decimals: int = 18
    closed_at: float | None = None
    exit_price: float | None = None
    reason: str | None = None


@dataclass
class TraderConfig:
    size_usd: float = 2.0
    max_concurrent: int = 3
    bankroll_usd: float = 50.0
    # Refuses to open anything once realised losses reach this. A hard floor the
    # strategy's own maths cannot argue past.
    max_total_loss_usd: float = 25.0
    # Live runs price the entry off a fresh quote, because a film row can be 30s
    # stale. Replaying an OLD film must not do that: it would pair a historical
    # exit price against an entry quoted at today's price. The first replay did
    # exactly that and reported $26 of profit on a $2 position capped at $10.
    use_live_quote: bool = True


@dataclass
class TraderState:
    """Everything that must survive a restart."""
    open_positions: dict[str, LaunchPosition] = field(default_factory=dict)
    traded_tokens: set[str] = field(default_factory=set)
    realised_pnl_usd: float = 0.0
    film_offset: int = 0


def load_state(path: Path) -> TraderState:
    if not path.exists():
        return TraderState()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return TraderState(
        open_positions={k: LaunchPosition(**v)
                        for k, v in raw.get("open_positions", {}).items()},
        traded_tokens=set(raw.get("traded_tokens", [])),
        realised_pnl_usd=raw.get("realised_pnl_usd", 0.0),
        film_offset=raw.get("film_offset", 0))


def save_state(path: Path, state: TraderState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "open_positions": {k: vars(v) for k, v in state.open_positions.items()},
        "traded_tokens": sorted(state.traded_tokens),
        "realised_pnl_usd": state.realised_pnl_usd,
        "film_offset": state.film_offset,
    }, indent=2), encoding="utf-8")
    tmp.replace(path)      # atomic: a crash mid-write must not lose open positions


def read_new_film_rows(path: Path, offset: int) -> tuple[list[dict], int]:
    """Rows appended since `offset`, and the new offset.

    A partial last line is left for the next pass — the collector appends while
    this reads, so a torn line is normal, not corruption.
    """
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if size < offset:
        offset = 0                      # file was rotated or truncated
    with open(path, "rb") as f:
        f.seek(offset)
        blob = f.read()
    text = blob.decode("utf-8", errors="ignore")
    consumed = offset + len(blob)
    if not text.endswith("\n"):
        cut = text.rfind("\n")
        if cut == -1:
            return [], offset           # no complete line yet
        consumed = offset + len(text[:cut + 1].encode("utf-8"))
        text = text[:cut + 1]
    rows = []
    for line in text.splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("film_line_unparseable")
    return rows, consumed


def should_enter(sample: dict, arm_price: float) -> bool:
    price = sample.get("price")
    liq = sample.get("liq")
    return bool(
        arm_price and price
        and price >= arm_price * ENTRY_MULTIPLE
        and liq is not None and liq >= MIN_ENTRY_LIQ_USD)


def exit_reason(pos: LaunchPosition, price: float | None, liq: float | None,
                now: float) -> str | None:
    """Take profit, time out, or nothing. Never a stop loss — it cannot fill."""
    if liq is not None and liq < DEAD_LIQ_USD:
        return "dead"
    if price and price >= pos.entry_price * TAKE_PROFIT_MULTIPLE:
        return "take_profit"
    if now - pos.opened_at >= MAX_HOLD_S:
        return "max_hold"
    return None


class LaunchTrader:
    def __init__(self, cfg: TraderConfig, state: TraderState,
                 executors: dict | None, state_path: Path, journal_path: Path,
                 dry_run: bool = True) -> None:
        self._cfg = cfg
        self._state = state
        self._executors = executors
        self._state_path = state_path
        self._journal_path = journal_path
        self._dry_run = dry_run
        self._arm_prices: dict[str, float] = {}

    # ---------- gates ----------

    def can_open(self, token: str) -> tuple[bool, str]:
        if token in self._state.open_positions:
            return False, "already_open"
        if token in self._state.traded_tokens:
            return False, "already_traded"      # one shot per token, ever
        if len(self._state.open_positions) >= self._cfg.max_concurrent:
            return False, "no_slot"
        if -self._state.realised_pnl_usd >= self._cfg.max_total_loss_usd:
            return False, "loss_limit"
        exposure = sum(p.usd_size for p in self._state.open_positions.values())
        if exposure + self._cfg.size_usd > self._cfg.bankroll_usd:
            return False, "bankroll"
        return True, ""

    # ---------- execution ----------

    def _buy(self, token: str, symbol: str, price: float,
             now: float) -> LaunchPosition | None:
        size = self._cfg.size_usd
        if self._dry_run or not self._executors:
            amount = size / price
        else:
            register_discovered(symbol, token, 18)
            ranked = rank_backends(self._executors, "USDT", symbol, size)
            if not ranked:
                log.warning("launch_buy_no_route", token=symbol)
                return None
            result = self._executors[ranked[0]].swap("USDT", symbol, size)
            wei = (getattr(result, "received_out_wei", 0)
                   or getattr(result, "expected_out_wei", 0))
            amount = wei / (10 ** 18)
            if amount <= 0:
                log.warning("launch_buy_zero_fill", token=symbol)
                return None
        # Entry price is what was actually PAID, not the price that triggered the
        # signal. The 6x target must be measured from the fill or it is a target
        # against a price we never got.
        # opened_at comes from the caller's clock, not time.time(): the hold
        # limit is measured against it, and a second clock source makes the 4h
        # exit fire against a different timeline than the one that opened it.
        return LaunchPosition(token_address=token, symbol=symbol,
                              entry_price=size / amount, usd_size=size,
                              token_amount=amount, opened_at=now)

    def _sell(self, pos: LaunchPosition) -> bool:
        if self._dry_run or not self._executors:
            return True
        ranked = rank_backends(self._executors, pos.symbol, "USDT",
                               pos.token_amount)
        for backend in ranked:
            try:
                self._executors[backend].swap(pos.symbol, "USDT",
                                              pos.token_amount)
                return True
            except Exception as e:      # noqa: BLE001 — try every backend
                log.warning("launch_sell_failed", token=pos.symbol,
                            backend=backend, error=str(e))
        log.error("launch_sell_all_backends_failed", token=pos.symbol)
        return False

    # ---------- the loop's two halves ----------

    def on_sample(self, row: dict, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        token = row.get("token_address")
        if not token:
            return None
        if row.get("event") == "arm" and row.get("price"):
            self._arm_prices[token] = row["price"]
            return None
        if row.get("event") != "sample":
            return None

        pos = self._state.open_positions.get(token)
        if pos is not None:
            reason = exit_reason(pos, row.get("price"), row.get("liq"), now)
            return self.close(pos, row.get("price"), reason, now) if reason else None

        arm_price = self._arm_prices.get(token)
        if arm_price is None or not should_enter(row, arm_price):
            return None
        ok, why = self.can_open(token)
        if not ok:
            log.debug("launch_entry_skipped", token=token, reason=why)
            return None
        return self.open(token, row, now)

    def open(self, token: str, sample: dict, now: float) -> str | None:
        # Price the trade off a LIVE quote, never the film row — a sample can be
        # up to 30s stale and this is the moment real money commits.
        if self._cfg.use_live_quote:
            stats = get_pair_stats(token) or {}
            price = stats.get("price_usd") or sample.get("price")
            liq = stats.get("liquidity_usd")
        else:
            price, liq = sample.get("price"), sample.get("liq")
        if not price or (liq is not None and liq < MIN_ENTRY_LIQ_USD):
            log.info("launch_entry_aborted_stale", token=token, liq=liq)
            return None
        pos = self._buy(token, sample.get("symbol") or token[:10], price, now)
        if pos is None:
            return None
        self._state.open_positions[token] = pos
        self._state.traded_tokens.add(token)
        save_state(self._state_path, self._state)
        log.info("launch_position_opened", token=pos.symbol,
                 entry=pos.entry_price, size=pos.usd_size,
                 open_now=len(self._state.open_positions))
        return "opened"

    def close(self, pos: LaunchPosition, price: float | None, reason: str,
              now: float) -> str:
        # A dead pool has no buyer. Book the loss and stop — retrying a sell into
        # drained liquidity burns gas on every tick for nothing.
        if reason != "dead" and not self._sell(pos):
            return "sell_failed"        # keep it open; the next sample retries
        proceeds = 0.0 if reason == "dead" else (price or 0.0) * pos.token_amount
        pnl = proceeds - pos.usd_size
        self._state.realised_pnl_usd += pnl
        self._state.open_positions.pop(pos.token_address, None)
        pos.closed_at, pos.exit_price, pos.reason = now, price, reason
        save_state(self._state_path, self._state)
        self._journal(vars(pos) | {"pnl_usd": round(pnl, 4)})
        log.info("launch_position_closed", token=pos.symbol, reason=reason,
                 pnl_usd=round(pnl, 4),
                 realised=round(self._state.realised_pnl_usd, 4))
        return reason

    def sweep_timeouts(self, now: float | None = None) -> int:
        """Close positions past MAX_HOLD_S even if their film stopped arriving —
        the collector disarms a token at 4h, so the sample stream that would
        otherwise trigger the exit goes silent exactly when it is needed."""
        now = time.time() if now is None else now
        closed = 0
        for pos in list(self._state.open_positions.values()):
            if now - pos.opened_at < MAX_HOLD_S:
                continue
            stats = (get_pair_stats(pos.token_address) or {}
                     if self._cfg.use_live_quote else {})
            liq = stats.get("liquidity_usd")
            reason = "dead" if (liq is not None and liq < DEAD_LIQ_USD) else "max_hold"
            self.close(pos, stats.get("price_usd"), reason, now)
            closed += 1
        return closed

    def _journal(self, row: dict) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def run(films_path: Path, state_path: Path, journal_path: Path,
        cfg: TraderConfig, executors: dict | None, dry_run: bool,
        once: bool = False, interval_s: float = 5.0) -> None:
    state = load_state(state_path)
    # A missing film file is indistinguishable from "no new samples yet", so the
    # trader would run forever on zero signals looking healthy. Fail loudly.
    if not films_path.exists():
        raise FileNotFoundError(
            f"no film stream at {films_path} — the collector writes it, and "
            f"without it this trader can never see a signal")
    trader = LaunchTrader(cfg, state, executors, state_path, journal_path,
                          dry_run=dry_run)
    # An open position from before a restart has no arm price in memory, but it
    # does not need one — only entries consult _arm_prices.
    log.info("launch_trader_start", dry_run=dry_run, size_usd=cfg.size_usd,
             max_concurrent=cfg.max_concurrent,
             resumed_positions=len(state.open_positions))
    while True:
        rows, state.film_offset = read_new_film_rows(films_path,
                                                     state.film_offset)
        clock = None
        for row in rows:
            # The sample's own timestamp is the clock. Live it is seconds old, so
            # this is simply more accurate than time.time(); replaying an old
            # film it is the only coherent choice — on wall-clock time every
            # replayed position is instantly past the 4h hold limit.
            clock = row.get("ts") or clock
            trader.on_sample(row, now=clock)
        trader.sweep_timeouts(now=clock)
        save_state(state_path, state)
        if once:
            return
        time.sleep(interval_s)


def main() -> None:
    import argparse
    root = ROOT
    ap = argparse.ArgumentParser(description="Launch method trader")
    ap.add_argument("--films", default=str(root / "data" / "launch_collector"
                                           / "films.jsonl"))
    ap.add_argument("--state", default=str(root / "data" / "launch_trader"
                                           / "state.json"))
    ap.add_argument("--journal", default=str(root / "data" / "launch_trader"
                                             / "trades.jsonl"))
    ap.add_argument("--size-usd", type=float, default=2.0)
    ap.add_argument("--max-concurrent", type=int, default=3)
    ap.add_argument("--bankroll", type=float, default=50.0)
    ap.add_argument("--max-loss", type=float, default=25.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--replay", action="store_true",
                    help="replay an old film: price entries from the film row "
                         "instead of a live quote, and run on the film's clock")
    # Live money is opt-IN. Every other flag defaults to the safe value, so a
    # mistyped command records instead of spending.
    ap.add_argument("--live", action="store_true",
                    help="place REAL orders (default: simulate fills)")
    args = ap.parse_args()

    executors = None
    if args.live:
        from eth_account import Account

        from ..execution.oneinch import OneInch
        from ..execution.openocean import OpenOcean
        from ..execution.pancakeswap import PancakeSwap
        account = Account.from_key(settings.agent_private_key)
        executors = {
            "1inch": OneInch(account=account, dry_run=False),
            "openocean": OpenOcean(account=account, dry_run=False),
            "pancake": PancakeSwap(account=account, dry_run=False,
                                   slippage_bps=1500),
        }

    run(Path(args.films), Path(args.state), Path(args.journal),
        TraderConfig(size_usd=args.size_usd, max_concurrent=args.max_concurrent,
                     bankroll_usd=args.bankroll,
                     max_total_loss_usd=args.max_loss,
                     use_live_quote=not args.replay),
        executors, dry_run=not args.live, once=args.once)


if __name__ == "__main__":
    main()
