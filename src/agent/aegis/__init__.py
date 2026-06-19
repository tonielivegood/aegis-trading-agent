"""Aegis event-driven alpha radar.

An AI radar over the official 149 eligible-token universe: detect early public
catalysts (event_signal_scanner), confirm them with 5-minute market anomalies
(volume_anomaly_detector), and only then propose tiny, risk-gated positions
(strategy.event_driven_alpha_momentum). Everything here is offline-testable and
DRY_RUN-gated; this package never signs or broadcasts a transaction.
"""
