"""Multi-core scoring pool.

* ``ProcessPoolExecutor`` across the phone's CPU cores (forkserver/spawn, not
  fork, so it is safe next to the web server's threads). If processes are not
  available (some Termux/Android builds lack POSIX semaphores) it falls back to
  a ``ThreadPoolExecutor``; ``COMPUTE_POOL=thread|process|off`` forces a mode.
* Small batches run inline: process start-up costs more than a few dozen
  sniper rows.
* ``REMOTE_COMPUTE_URL`` (OFF by default): an HTTP endpoint you run yourself
  (``tools/remote_compute_server.py``) that may compute **model math only**.
  It receives game-state numbers (league, lead, time left, provider win %) —
  never keys, tickers, prices, bankroll or orders — and it never makes Kalshi
  calls. Kalshi hosts are refused as targets. All Kalshi traffic stays on the
  one in-app client and its shared rate budget; the remote box only does math.
  Any remote error or timeout falls back to local compute.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

MIN_PARALLEL = 64
REMOTE_ALLOWED_FIELDS = ("league", "status", "lead", "frac_left", "live_win_p", "prior_p")
REMOTE_KINDS = {"sniper_prob"}
_FORBIDDEN_HOST_PARTS = ("kalshi",)


def remote_url_ok(url: str) -> tuple[bool, str]:
    try:
        u = urlparse(url)
    except ValueError:
        return False, "bad URL"
    if u.scheme not in {"http", "https"} or not u.hostname:
        return False, "REMOTE_COMPUTE_URL must be http(s)://host[:port]/path"
    host = u.hostname.lower()
    if any(part in host for part in _FORBIDDEN_HOST_PARTS):
        return False, "REMOTE_COMPUTE_URL may not point at Kalshi"
    return True, ""


def _make_process_pool(workers: int) -> Executor:
    import multiprocessing as mp

    methods = mp.get_all_start_methods()
    ctx = mp.get_context("forkserver" if "forkserver" in methods else "spawn")
    ex = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
    # Probe once: Android without sem_open fails here or on first submit.
    ex.submit(int, 1).result(timeout=30)
    return ex


class ComputePool:
    def __init__(self, mode: Optional[str] = None, workers: Optional[int] = None,
                 remote_url: Optional[str] = None, min_parallel: int = MIN_PARALLEL,
                 post: Optional[Callable[[str, dict, float], Any]] = None):
        self.mode_wanted = (mode or os.getenv("COMPUTE_POOL", "auto")).strip().lower()
        self.workers = max(1, int(workers or os.getenv("COMPUTE_WORKERS", 0) or (os.cpu_count() or 1)))
        self.min_parallel = int(min_parallel)
        self.remote_url = (remote_url if remote_url is not None else os.getenv("REMOTE_COMPUTE_URL", "")).strip()
        self.remote_timeout_s = float(os.getenv("REMOTE_COMPUTE_TIMEOUT_S", "3"))
        self._post = post or self._http_post
        self._ex: Optional[Executor] = None
        self.kind = "inline"
        self.fallback_reason: Optional[str] = None
        self._lock = threading.Lock()
        self.stats = {"inline": 0, "parallel": 0, "remote_ok": 0, "remote_fail": 0}
        if self.remote_url:
            ok, why = remote_url_ok(self.remote_url)
            if not ok:
                log.warning("remote compute disabled: %s", why)
                self.fallback_reason = why
                self.remote_url = ""

    # ----- local -----

    def _executor(self) -> Optional[Executor]:
        if self.mode_wanted == "off" or self.workers <= 1 and self.mode_wanted != "thread":
            return None
        with self._lock:
            if self._ex is not None:
                return self._ex
            if self.mode_wanted in {"auto", "process"}:
                try:
                    self._ex = _make_process_pool(self.workers)
                    self.kind = "process"
                    return self._ex
                except Exception as exc:  # Termux without sem_open, sandboxed builds, ...
                    self.fallback_reason = f"process pool unavailable: {type(exc).__name__}: {exc}"
                    log.info("compute: %s; using threads", self.fallback_reason)
            self._ex = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="compute")
            self.kind = "thread"
            return self._ex

    def map(self, fn: Callable, items: Iterable, chunksize: int = 16) -> list:
        items = list(items)
        if len(items) < self.min_parallel:
            self.stats["inline"] += 1
            return [fn(x) for x in items]
        ex = self._executor()
        if ex is None:
            self.stats["inline"] += 1
            return [fn(x) for x in items]
        try:
            self.stats["parallel"] += 1
            return list(ex.map(fn, items, chunksize=max(1, chunksize)))
        except Exception as exc:  # BrokenProcessPool, pickling errors -> local
            log.warning("compute pool failed (%s); running inline", exc)
            self._reset(f"pool error: {exc}")
            return [fn(x) for x in items]

    def run_cpu(self, fn: Callable, *args, min_size: int = 0, timeout_s: Optional[float] = None, **kwargs):
        """Run one CPU-heavy call off the main process when ``min_size`` is big
        enough to be worth it; otherwise (or on any failure) run it here."""
        if min_size < self.min_parallel * 10:
            return fn(*args, **kwargs)
        ex = self._executor()
        if ex is None:
            return fn(*args, **kwargs)
        try:
            return ex.submit(fn, *args, **kwargs).result(timeout=timeout_s)
        except Exception as exc:
            log.warning("run_cpu failed (%s); running inline", exc)
            self._reset(f"run_cpu error: {exc}")
            return fn(*args, **kwargs)

    def _reset(self, why: str) -> None:
        with self._lock:
            ex, self._ex = self._ex, None
            self.fallback_reason = why
            if self.kind == "process":
                self.mode_wanted = "thread"  # don't keep re-spawning a broken pool
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    # ----- remote (opt-in, math only) -----

    def _http_post(self, url: str, body: dict, timeout: float) -> Any:
        import requests

        r = requests.post(url, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def remote(self, kind: str, items: list) -> Optional[list]:
        if not self.remote_url or kind not in REMOTE_KINDS:
            return None
        safe = [{k: it.get(k) for k in REMOTE_ALLOWED_FIELDS} for it in items]
        try:
            out = self._post(self.remote_url, {"kind": kind, "items": safe}, self.remote_timeout_s)
            res = out.get("results") if isinstance(out, dict) else None
            if not isinstance(res, list) or len(res) != len(items):
                raise ValueError("bad remote response")
            vals = [max(0.02, min(0.98, float(v))) for v in res]
            self.stats["remote_ok"] += 1
            return vals
        except Exception as exc:
            self.stats["remote_fail"] += 1
            log.info("remote compute failed (%s); local fallback", exc)
            return None

    # ----- jobs -----

    def sniper_probs(self, items: list) -> list:
        from app.trading.sniper import sniper_prob

        got = self.remote("sniper_prob", items)
        if got is not None:
            return got
        return self.map(sniper_prob, items)

    def status(self) -> dict:
        return {
            "kind": self.kind if self._ex is not None else ("off" if self.mode_wanted == "off" else "idle"),
            "mode": self.mode_wanted,
            "workers": self.workers,
            "cpu_count": os.cpu_count(),
            "min_parallel": self.min_parallel,
            "remote": bool(self.remote_url),
            "remote_host": urlparse(self.remote_url).hostname if self.remote_url else None,
            "fallback_reason": self.fallback_reason,
            "stats": dict(self.stats),
        }

    def shutdown(self) -> None:
        self._reset("shutdown")


_POOL: Optional[ComputePool] = None


def get_pool() -> ComputePool:
    global _POOL
    if _POOL is None:
        _POOL = ComputePool()
    return _POOL
