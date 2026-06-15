"""Typed signal data structures — the prompt-injection firewall boundary.

Everything that crosses out of the signal layer is one of these models: bounded
numbers and an enum direction. No free text, no callables, nothing executable.
Validators clamp numeric fields so out-of-range model output can't propagate.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

Direction = Literal["BUY", "SELL", "HOLD"]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


class SentimentScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: float        # [-1, 1]
    confidence: float   # [0, 1]

    @field_validator("score")
    @classmethod
    def _clamp_score(cls, v: float) -> float:
        return _clamp(v, -1.0, 1.0)

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, v: float) -> float:
        return _clamp(v, 0.0, 1.0)


NEUTRAL_SENTIMENT = SentimentScore(score=0.0, confidence=0.0)


class SignalBundle(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    momentum_score: float    # [-1, 1]
    sentiment_score: float   # [-1, 1]
    confidence: float        # [0, 1]
    combined_score: float    # [-1, 1]
    direction: Direction

    @field_validator("momentum_score", "sentiment_score", "combined_score")
    @classmethod
    def _clamp_unit(cls, v: float) -> float:
        return _clamp(v, -1.0, 1.0)

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, v: float) -> float:
        return _clamp(v, 0.0, 1.0)
