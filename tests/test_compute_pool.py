"""Speed pack item 8: multi-core pool with thread fallback + opt-in remote math hook."""
from __future__ import annotations

import pytest

from app.compute import pool as P
from app.trading.sniper import score_batch, score_candidate, sniper_prob

ITEM = {"ticker": "KXNBAGAME-26NOV09WASDET-DET", "league": "nba", "status": "inprogress", "lead": 9,
        "frac_left": 0.04, "live_win_p": None, "prior_p": None, "yes_ask": 0.86, "no_ask": 0.16,
        "bankroll": 1000.0, "fraction": 0.5, "max_fraction": 0.05, "fee_multiplier": 1.0, "ev_min": 0.03}


def _sq(x):
    return x * x


def test_small_batches_run_inline():
    p = P.ComputePool(mode="auto", workers=4, remote_url="")
    assert p.map(_sq, range(5)) == [0, 1, 4, 9, 16]
    assert p.stats["inline"] == 1 and p.status()["kind"] == "idle"


def test_process_pool_or_thread_fallback_matches_inline():
    p = P.ComputePool(mode="auto", workers=2, remote_url="", min_parallel=4)
    try:
        assert p.map(_sq, range(100)) == [i * i for i in range(100)]
        assert p.kind in {"process", "thread"}
    finally:
        p.shutdown()


def test_thread_fallback_when_processes_unavailable(monkeypatch):
    def boom(workers):
        raise NotImplementedError("sem_open not available (Android)")

    monkeypatch.setattr(P, "_make_process_pool", boom)
    p = P.ComputePool(mode="auto", workers=2, remote_url="", min_parallel=4)
    try:
        assert p.map(_sq, range(10)) == [i * i for i in range(10)]
        assert p.kind == "thread" and "sem_open" in p.status()["fallback_reason"]
    finally:
        p.shutdown()


def test_remote_off_by_default_and_never_kalshi(monkeypatch):
    monkeypatch.delenv("REMOTE_COMPUTE_URL", raising=False)
    assert P.ComputePool().remote_url == ""
    for bad in ("https://api.elections.kalshi.com/x", "https://external-api.kalshi.com/c", "ftp://box/c"):
        p = P.ComputePool(remote_url=bad)
        assert p.remote_url == "" and p.fallback_reason


def test_remote_sends_only_game_state_and_falls_back():
    sent = []

    def post(url, body, timeout):
        sent.append(body)
        return {"results": [0.9 for _ in body["items"]]}

    p = P.ComputePool(remote_url="http://192.168.1.20:8765/compute", post=post)
    assert p.sniper_probs([ITEM]) == [0.9]
    keys = set(sent[0]["items"][0])
    assert keys == set(P.REMOTE_ALLOWED_FIELDS)
    assert not keys & {"ticker", "bankroll", "yes_ask", "no_ask"}

    def fail(url, body, timeout):
        raise TimeoutError("box asleep")

    p2 = P.ComputePool(remote_url="http://192.168.1.20:8765/compute", post=fail)
    assert p2.sniper_probs([ITEM]) == [sniper_prob(ITEM)] and p2.stats["remote_fail"] == 1


def test_score_batch_with_pool_matches_direct():
    p = P.ComputePool(remote_url="")
    got = score_batch([ITEM], prob_fn=p.sniper_probs)
    assert got == [score_candidate(ITEM)]


def test_remote_server_compute_is_math_only():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "rcs", Path(__file__).resolve().parents[1] / "tools" / "remote_compute_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.compute({"kind": "sniper_prob", "items": [{k: ITEM[k] for k in P.REMOTE_ALLOWED_FIELDS}]})
    assert out["results"] == [sniper_prob(ITEM)]
    with pytest.raises(ValueError):
        mod.compute({"kind": "place_order", "items": []})
    src = (Path(__file__).resolve().parents[1] / "tools" / "remote_compute_server.py").read_text()
    assert "requests" not in src and "KalshiClient" not in src


def test_run_cpu_small_is_inline():
    p = P.ComputePool(remote_url="", workers=2)
    assert p.run_cpu(_sq, 7, min_size=10) == 49 and p._ex is None
