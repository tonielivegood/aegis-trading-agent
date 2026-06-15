"""Combine momentum + sentiment into per-token SignalBundles.

Output is a list of bounded, validated SignalBundle objects — the only thing the
strategy layer ever consumes from signals. Sentiment is optional; with no
sentiment, momentum alone drives the decision.
"""
from __future__ import annotations

from . import momentum
from .signal_schema import NEUTRAL_SENTIMENT, SentimentScore, SignalBundle

BUY_THRESHOLD = 0.15
SELL_THRESHOLD = -0.15
W_MOMENTUM = 0.6
W_SENTIMENT = 0.4


def _direction(combined: float) -> str:
    if combined >= BUY_THRESHOLD:
        return "BUY"
    if combined <= SELL_THRESHOLD:
        return "SELL"
    return "HOLD"


def generate(
    symbols: list[str],
    quotes: dict[str, dict],
    sentiments: dict[str, SentimentScore] | None = None,
) -> list[SignalBundle]:
    sentiments = sentiments or {}
    bundles: list[SignalBundle] = []

    for sym in symbols:
        q = quotes.get(sym) or {}
        mom = momentum.compute_momentum(
            q.get("percent_change_1h"), q.get("percent_change_24h"), q.get("percent_change_7d")
        )
        sent = sentiments.get(sym, NEUTRAL_SENTIMENT)

        if sent.confidence > 0:
            combined = W_MOMENTUM * mom + W_SENTIMENT * sent.score
            confidence = min(1.0, abs(combined) * 0.5 + sent.confidence * 0.5)
        else:
            combined = mom
            confidence = abs(mom)

        bundles.append(SignalBundle(
            symbol=sym,
            momentum_score=mom,
            sentiment_score=sent.score,
            confidence=confidence,
            combined_score=max(-1.0, min(1.0, combined)),
            direction=_direction(combined),
        ))
    return bundles
