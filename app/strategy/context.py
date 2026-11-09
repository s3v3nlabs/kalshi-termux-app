"""Context adapters: NOAA/NWS weather and public crypto volatility.

Both fail soft: no parse, a network error, or a missing number gives ``None``,
the feature is 0 in z, and the row says why. No API keys.

Weather (``api.weather.gov``)
-----------------------------
Kalshi daily temperature markets: series ``KXHIGH<CITY>`` / ``KXHIGHT<CITY>``
(daily max) and ``KXLOWT<CITY>`` / ``KXLOW<CITY>`` (daily min), event date in the
event ticker (``KXHIGHNY-26NOV09``), strike from the structured fields
``strike_type`` (greater / less / between), ``floor_strike``, ``cap_strike``.
Kalshi settles on whole degrees, so ">64" means T ≥ 65 (T > 64.5), "<57" means
T ≤ 56 (T < 56.5), and between a..b means a−0.5 ≤ T ≤ b+0.5.

Estimate: the NWS hourly forecast max (or min) over that local day, combined
with today's observations at the settlement station so far (an observed
high already reached counts, and an observed extreme that already crosses the
strike pins P(YES) at 0.99 / 0.01). P(YES) = normal CDF around that estimate with
sigma by lead time (2.5°F same day, 3.0 next day, +0.5/day after, cap 6).
``weather_delta = logit(P_weather) − logit(market P)`` (same spirit as
``espn_delta``).

Crypto (Coinbase Exchange public candles, no key)
-------------------------------------------------
For ``KXBTC*``, ``KXETH*``, ``KXSOL*``, ``KXXRP*``, ``KXDOGE*`` markets: hourly
candles → realized vol of the last 24 h vs the last ~12 days of hours.
``vol_z = log(rv_24h / rv_base) / 0.5`` clipped to [−3, 3]. It is signed by
what higher vol does to YES: YES needs a move away from spot (spot below a
"greater" strike, above a "less" strike, or outside a range) → +vol_z;
YES is "stay where it is" → −vol_z. That is ``crypto_vol_z``.
"""
from __future__ import annotations

import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import requests

from app.strategy.model import logit

USER_AGENT = "kalshi-termux-portal/2 (operator console; no contact)"
TIMEOUT_S = 6.0
FAIL_TTL_S = 300.0

# Settlement stations (NWS climate reports Kalshi cites) + coordinates.
CITIES: dict[str, dict[str, Any]] = {
    "NY": {"name": "New York (Central Park)", "station": "KNYC", "lat": 40.7790, "lon": -73.9692},
    "NYC": {"name": "New York (Central Park)", "station": "KNYC", "lat": 40.7790, "lon": -73.9692},
    "CHI": {"name": "Chicago (Midway)", "station": "KMDW", "lat": 41.7868, "lon": -87.7522},
    "LAX": {"name": "Los Angeles (LAX)", "station": "KLAX", "lat": 33.9382, "lon": -118.3866},
    "MIA": {"name": "Miami", "station": "KMIA", "lat": 25.7881, "lon": -80.3169},
    "AUS": {"name": "Austin", "station": "KAUS", "lat": 30.1830, "lon": -97.6799},
    "DEN": {"name": "Denver", "station": "KDEN", "lat": 39.8466, "lon": -104.6562},
    "PHIL": {"name": "Philadelphia", "station": "KPHL", "lat": 39.8733, "lon": -75.2268},
    "DAL": {"name": "Dallas (DFW)", "station": "KDFW", "lat": 32.8998, "lon": -97.0403},
    "HOU": {"name": "Houston (Hobby)", "station": "KHOU", "lat": 29.6375, "lon": -95.2825},
    "SEA": {"name": "Seattle", "station": "KSEA", "lat": 47.4447, "lon": -122.3144},
    "SFO": {"name": "San Francisco", "station": "KSFO", "lat": 37.6197, "lon": -122.3656},
    "ATL": {"name": "Atlanta", "station": "KATL", "lat": 33.6407, "lon": -84.4277},
    "BOS": {"name": "Boston", "station": "KBOS", "lat": 42.3606, "lon": -71.0097},
    "LV": {"name": "Las Vegas", "station": "KLAS", "lat": 36.0719, "lon": -115.1634},
    "PHX": {"name": "Phoenix", "station": "KPHX", "lat": 33.4342, "lon": -112.0116},
    "DC": {"name": "Washington (DCA)", "station": "KDCA", "lat": 38.8483, "lon": -77.0342},
    "NOLA": {"name": "New Orleans", "station": "KMSY", "lat": 29.9934, "lon": -90.2580},
    "MIN": {"name": "Minneapolis", "station": "KMSP", "lat": 44.8831, "lon": -93.2289},
    "OKC": {"name": "Oklahoma City", "station": "KOKC", "lat": 35.3889, "lon": -97.6006},
    "SATX": {"name": "San Antonio", "station": "KSAT", "lat": 29.5337, "lon": -98.4698},
}
_WEATHER_SERIES = re.compile(r"^KX(?P<metric>HIGH|LOW)T?(?P<city>[A-Z]+)$")
_EVENT_DATE = re.compile(r"-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})")
_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}

CRYPTO_PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD", "DOGE": "DOGE-USD"}
_CRYPTO_SERIES = re.compile(r"^KX(?P<asset>BTC|ETH|SOL|XRP|DOGE)")
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{product}/candles?granularity=3600"
CRYPTO_SHORT_H = 24
CRYPTO_VOL_SCALE = 0.5


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _series(market: dict) -> str:
    return str(market.get("event_ticker") or market.get("ticker") or "").split("-")[0].upper()


def strike_of(market: dict) -> Optional[dict]:
    st = str(market.get("strike_type") or "").lower()
    lo, hi = _f(market.get("floor_strike")), _f(market.get("cap_strike"))
    if st in {"greater", "greater_or_equal"} and lo is not None:
        return {"type": "greater", "floor": lo, "cap": None}
    if st in {"less", "less_or_equal"} and hi is not None:
        return {"type": "less", "floor": None, "cap": hi}
    if st == "between" and lo is not None and hi is not None:
        return {"type": "between", "floor": lo, "cap": hi}
    return None


class _Http:
    """Tiny cached JSON GET with failure caching. ``fetch`` is injectable."""

    def __init__(self, fetch: Optional[Callable[[str], Any]] = None, ttl: float = 900.0):
        self._fetch = fetch
        self.ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.ok = 0
        self.errors = 0

    def get(self, url: str, ttl: Optional[float] = None) -> Optional[Any]:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(url)
            if hit and now < hit[0]:
                return hit[1]
        try:
            if self._fetch is not None:
                data = self._fetch(url)
            else:
                r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"}, timeout=TIMEOUT_S)
                r.raise_for_status()
                data = r.json()
            self.ok += 1
            exp = now + (self.ttl if ttl is None else ttl)
        except Exception:
            self.errors += 1
            data = None
            exp = now + FAIL_TTL_S
        with self._lock:
            self._cache[url] = (exp, data)
        return data


# ---------------- weather ----------------


def parse_weather_market(market: dict) -> Optional[dict]:
    m = _WEATHER_SERIES.match(_series(market))
    if not m:
        return None
    city = CITIES.get(m.group("city"))
    if city is None:
        return None
    d = _EVENT_DATE.search(str(market.get("event_ticker") or market.get("ticker") or ""))
    strike = strike_of(market)
    if not d or strike is None or d.group("mon") not in _MONTHS:
        return None
    try:
        day = date(2000 + int(d.group("yy")), _MONTHS[d.group("mon")], int(d.group("dd")))
    except ValueError:
        return None
    return {"metric": "high" if m.group("metric") == "HIGH" else "low", "city": m.group("city"), **city, "date": day.isoformat(), "strike": strike}


def weather_sigma(lead_days: int) -> float:
    if lead_days <= 0:
        return 2.5
    return min(6.0, 3.0 + 0.5 * (lead_days - 1))


def p_yes_from_temp(estimate_f: float, strike: dict, sigma: float) -> float:
    if strike["type"] == "greater":
        return 1.0 - _norm_cdf((strike["floor"] + 0.5 - estimate_f) / sigma)
    if strike["type"] == "less":
        return _norm_cdf((strike["cap"] - 0.5 - estimate_f) / sigma)
    lo = (strike["floor"] - 0.5 - estimate_f) / sigma
    hi = (strike["cap"] + 0.5 - estimate_f) / sigma
    return _norm_cdf(hi) - _norm_cdf(lo)


def observed_bound(metric: str, observed: list[float], strike: dict) -> Optional[float]:
    """A daily max can only go up and a min only down. When today's
    observations already cross the strike, the answer is (nearly) known:
    0.99 / 0.01, never 1 / 0 (station rounding and the official climate
    report can still differ)."""
    if not observed:
        return None
    t, lo, hi = strike["type"], strike.get("floor"), strike.get("cap")
    if metric == "high":
        mx = max(observed)
        if t == "greater" and mx >= lo + 0.5:
            return 0.99
        if t in {"less", "between"} and mx > (hi - 0.5 if t == "less" else hi + 0.5):
            return 0.01
    else:
        mn = min(observed)
        if t == "less" and mn <= hi - 0.5:
            return 0.99
        if t in {"greater", "between"} and mn < (lo + 0.5 if t == "greater" else lo - 0.5):
            return 0.01
    return None


def _to_f(value: Any, unit: str) -> Optional[float]:
    v = _f(value)
    if v is None:
        return None
    return v * 9.0 / 5.0 + 32.0 if "degC" in unit or unit.upper() == "C" else v


class WeatherAdapter:
    def __init__(self, fetch: Optional[Callable[[str], Any]] = None, now: Optional[Callable[[], datetime]] = None):
        self.http = _Http(fetch, ttl=900.0)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def estimate(self, market: dict) -> Optional[dict]:
        w = parse_weather_market(market)
        if w is None:
            return None
        pts = self.http.get(f"https://api.weather.gov/points/{w['lat']:.4f},{w['lon']:.4f}", ttl=86400.0)
        props = (pts or {}).get("properties") or {}
        url, tzname = props.get("forecastHourly"), props.get("timeZone") or "America/New_York"
        if not url:
            return {**w, "error": "NWS points lookup failed", "p_yes": None}
        tz = ZoneInfo(tzname)
        day = date.fromisoformat(w["date"])
        temps = []
        for p in ((self.http.get(url) or {}).get("properties") or {}).get("periods") or []:
            try:
                st = datetime.fromisoformat(str(p["startTime"]))
            except (KeyError, ValueError):
                continue
            t = _to_f(p.get("temperature"), str(p.get("temperatureUnit") or "F"))
            if t is not None and st.astimezone(tz).date() == day:
                temps.append(t)
        now = self._now()
        local_today = now.astimezone(tz).date()
        observed = []
        if day == local_today:
            start = datetime.combine(day, datetime.min.time(), tz).astimezone(timezone.utc)
            obs = self.http.get(
                f"https://api.weather.gov/stations/{w['station']}/observations?start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}", ttl=600.0
            )
            for f in (obs or {}).get("features") or []:
                t = (f.get("properties") or {}).get("temperature") or {}
                v = _to_f(t.get("value"), str(t.get("unitCode") or ""))
                if v is not None:
                    observed.append(v)
        if day < local_today:
            return {**w, "error": "market date already passed", "p_yes": None}
        if not temps and not observed:
            return {**w, "error": "no forecast hours for that day", "p_yes": None}
        pool = temps + observed
        est = max(pool) if w["metric"] == "high" else min(pool)
        lead = (day - local_today).days
        sigma = weather_sigma(lead)
        p = p_yes_from_temp(est, w["strike"], sigma)
        settled_by_obs = observed_bound(w["metric"], observed, w["strike"])
        if settled_by_obs is not None:
            p = settled_by_obs
        p = max(0.01, min(0.99, p))
        word = "high" if w["metric"] == "high" else "low"
        return {
            **w,
            "p_yes": round(p, 4),
            "estimate_f": round(est, 1),
            "forecast_hours": len(temps),
            "observed_hours": len(observed),
            "observed_extreme_f": None if not observed else round(max(observed) if w["metric"] == "high" else min(observed), 1),
            "sigma_f": sigma,
            "lead_days": lead,
            "hint": f"NWS {word} for {w['name']} on {w['date']}: ~{est:.0f}°F (±{sigma:g}°F) → P(YES) ≈ {p * 100:.0f}%. A forecast, not a result.",
        }


def weather_delta(p_weather: Optional[float], market_p_yes: float) -> Optional[float]:
    if p_weather is None:
        return None
    return round(logit(p_weather) - logit(market_p_yes), 6)


# ---------------- crypto ----------------


def parse_crypto_market(market: dict) -> Optional[dict]:
    m = _CRYPTO_SERIES.match(_series(market))
    if not m:
        return None
    strike = strike_of(market)
    if strike is None:
        return None
    return {"asset": m.group("asset"), "product": CRYPTO_PRODUCTS[m.group("asset")], "strike": strike}


def realized_vol(closes: list[float]) -> Optional[float]:
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 3:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def vol_direction(spot: float, strike: dict) -> int:
    """+1 when YES needs a move away from spot (more vol helps YES), else -1."""
    if strike["type"] == "greater":
        return 1 if spot <= strike["floor"] else -1
    if strike["type"] == "less":
        return 1 if spot >= strike["cap"] else -1
    return -1 if strike["floor"] <= spot <= strike["cap"] else 1


class CryptoAdapter:
    def __init__(self, fetch: Optional[Callable[[str], Any]] = None):
        self.http = _Http(fetch, ttl=300.0)

    def vol_stats(self, product: str) -> Optional[dict]:
        rows = self.http.get(COINBASE_CANDLES.format(product=product))
        if not isinstance(rows, list) or len(rows) < CRYPTO_SHORT_H + 24:
            return None
        rows = sorted((r for r in rows if isinstance(r, list) and len(r) >= 5), key=lambda r: r[0])
        closes = [float(r[4]) for r in rows]
        short = realized_vol(closes[-(CRYPTO_SHORT_H + 1) :])
        base = realized_vol(closes[: -CRYPTO_SHORT_H])
        if not short or not base:
            return None
        z = max(-3.0, min(3.0, math.log(short / base) / CRYPTO_VOL_SCALE))
        return {"spot": closes[-1], "rv_24h": short, "rv_base": base, "vol_z": round(z, 4), "hours": len(closes)}

    def context(self, market: dict) -> Optional[dict]:
        c = parse_crypto_market(market)
        if c is None:
            return None
        st = self.vol_stats(c["product"])
        if st is None:
            return {**c, "error": "Coinbase candles unavailable", "crypto_vol_z": None}
        sign = vol_direction(st["spot"], c["strike"])
        cz = round(sign * st["vol_z"], 4)
        return {
            **c,
            **st,
            "direction": sign,
            "crypto_vol_z": cz,
            "hint": (
                f"{c['asset']} spot ${st['spot']:,.0f}; 24h realized vol is {math.exp(st['vol_z'] * CRYPTO_VOL_SCALE):.2f}x its "
                f"{st['hours'] - CRYPTO_SHORT_H}h baseline ({'helps' if cz > 0 else 'works against'} YES). Context, not a forecast."
            ),
        }


class ContextAdapters:
    def __init__(self, weather: Optional[WeatherAdapter] = None, crypto: Optional[CryptoAdapter] = None):
        self.weather = weather or WeatherAdapter()
        self.crypto = crypto or CryptoAdapter()

    def stats(self) -> dict:
        return {
            "weather_fetch_ok": self.weather.http.ok,
            "weather_fetch_errors": self.weather.http.errors,
            "crypto_fetch_ok": self.crypto.http.ok,
            "crypto_fetch_errors": self.crypto.http.errors,
        }


_default: Optional[ContextAdapters] = None
_default_lock = threading.Lock()


def get_context() -> ContextAdapters:
    global _default
    with _default_lock:
        if _default is None:
            _default = ContextAdapters()
        return _default
