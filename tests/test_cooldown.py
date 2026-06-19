"""TDD for per-token re-entry cooldown (anti-whipsaw)."""
from src.agent.aegis.cooldown import CooldownBook


def test_no_cooldown_when_empty():
    assert CooldownBook().cooling_down(now=1000.0, cooldown_s=3600) == set()


def test_recent_exit_is_cooling_down():
    cb = CooldownBook()
    cb.record_exit("AAA", now=1000.0)
    assert cb.cooling_down(now=1000.0 + 1800, cooldown_s=3600) == {"AAA"}   # 30m < 60m


def test_expired_cooldown_clears():
    cb = CooldownBook()
    cb.record_exit("AAA", now=1000.0)
    assert cb.cooling_down(now=1000.0 + 4000, cooldown_s=3600) == set()      # 66m > 60m


def test_multiple_tokens_independent():
    cb = CooldownBook()
    cb.record_exit("AAA", now=1000.0)
    cb.record_exit("BBB", now=1000.0 + 3000)
    cooling = cb.cooling_down(now=1000.0 + 3700, cooldown_s=3600)
    assert cooling == {"BBB"}        # AAA expired (61m > 60m), BBB fresh (~12m)


def test_round_trip_persistence(tmp_path):
    p = tmp_path / "cooldown.json"
    cb = CooldownBook()
    cb.record_exit("AAA", now=1000.0)
    cb.save(p)
    loaded = CooldownBook.load(p)
    assert loaded.cooling_down(now=1000.0 + 60, cooldown_s=3600) == {"AAA"}


def test_prune_drops_stale_entries():
    cb = CooldownBook()
    cb.record_exit("AAA", now=1000.0)
    cb.record_exit("BBB", now=1000.0 + 5000)
    cb.prune(now=1000.0 + 5100, cooldown_s=3600)
    assert set(cb.last_exit) == {"BBB"}
