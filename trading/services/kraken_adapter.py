import base64
import hashlib
import hmac
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import requests
import redis


logger = logging.getLogger(__name__)


API_BASE = os.getenv("KRAKEN_API_BASE", "https://api.kraken.com")
PUBLIC_BASE = f"{API_BASE}/0/public"
PRIVATE_BASE = f"{API_BASE}/0/private"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float):
        self.rate = rate_per_sec
        self.capacity = capacity
        self._tokens = capacity
        self._last = time.time()

    def consume(self, weight: float) -> float:
        now = time.time()
        elapsed = now - self._last
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last = now
        if self._tokens >= weight:
            self._tokens -= weight
            return 0.0
        needed = weight - self._tokens
        wait_time = needed / self.rate if self.rate > 0 else 0.2
        self._tokens = 0.0
        self._last = now
        return wait_time


class SharedRateLimiter:
    """Redis-backed token bucket shared across worker processes."""

    def __init__(self, redis_url: str, base_rate: float, min_rate: float, capacity: float, key_prefix: str = "kraken_rate"):
        self.redis = redis.Redis.from_url(redis_url)
        self.base_rate = base_rate
        self.min_rate = min_rate
        self.capacity = capacity
        self.key_tokens = f"{key_prefix}_tokens"
        self.key_last = f"{key_prefix}_last"
        self.key_rate = f"{key_prefix}_current"
        # Init rate
        self.redis.setnx(self.key_rate, base_rate)
        self.redis.setnx(self.key_tokens, capacity)
        self.redis.setnx(self.key_last, time.time())
        logger.info("Using shared Redis rate limiter with prefix %s", key_prefix)

    def set_active_users(self, active_users: int):
        clamped = max(1, active_users)
        if clamped >= 5:
            rate = self.min_rate
        else:
            ratio = (clamped - 1) / 4
            rate = self.base_rate - ratio * (self.base_rate - self.min_rate)
        self.redis.set(self.key_rate, max(self.min_rate, min(self.base_rate, rate)))

    def consume(self, weight: float):
        while True:
            pipe = self.redis.pipeline()
            pipe.get(self.key_tokens)
            pipe.get(self.key_last)
            pipe.get(self.key_rate)
            tokens_raw, last_raw, rate_raw = pipe.execute()
            tokens = float(tokens_raw or 0)
            last = float(last_raw or time.time())
            rate = float(rate_raw or self.base_rate)
            now = time.time()
            elapsed = now - last
            tokens = min(self.capacity, tokens + elapsed * rate)
            if tokens >= weight:
                tokens -= weight
                pipe = self.redis.pipeline()
                pipe.set(self.key_tokens, tokens)
                pipe.set(self.key_last, now)
                pipe.execute()
                return
            needed = weight - tokens
            wait_time = needed / rate if rate > 0 else 0.2
            pipe = self.redis.pipeline()
            pipe.set(self.key_tokens, 0.0)
            pipe.set(self.key_last, now)
            pipe.execute()
            time.sleep(max(0.0, wait_time))

    def apply_penalty(self):
        self.redis.set(self.key_rate, self.min_rate)

    def snapshot(self) -> Dict[str, float]:
        pipe = self.redis.pipeline()
        pipe.get(self.key_tokens)
        pipe.get(self.key_rate)
        pipe.get(self.key_last)
        tokens_raw, rate_raw, last_raw = pipe.execute()
        return {
            "mode": "shared",
            "tokens": float(tokens_raw or 0),
            "rate_per_sec": float(rate_raw or self.base_rate),
            "capacity": self.capacity,
            "last": float(last_raw or time.time()),
        }


class LocalRateLimiter:
    """Process-local token bucket (fallback when Redis not configured)."""

    def __init__(self, base_rate: float, min_rate: float, capacity: float):
        self.base_rate = base_rate
        self.min_rate = min_rate
        self.capacity = capacity
        self._bucket = TokenBucket(base_rate, capacity)
        self._lock = threading.Lock()

    def set_active_users(self, active_users: int):
        clamped = max(1, active_users)
        if clamped >= 5:
            rate = self.min_rate
        else:
            ratio = (clamped - 1) / 4
            rate = self.base_rate - ratio * (self.base_rate - self.min_rate)
        with self._lock:
            self._bucket.rate = max(self.min_rate, min(self.base_rate, rate))

    def consume(self, weight: float):
        while True:
            with self._lock:
                wait_time = self._bucket.consume(weight)
            if wait_time <= 0:
                return
            time.sleep(wait_time)

    def apply_penalty(self):
        with self._lock:
            self._bucket.rate = self.min_rate

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            tokens = self._bucket._tokens
            rate = self._bucket.rate
            last = self._bucket._last
        return {
            "mode": "local",
            "tokens": tokens,
            "rate_per_sec": rate,
            "capacity": self.capacity,
            "last": last,
        }


class PerUserLimiter:
    """Optional per-user bucket layered on top of the global limiter."""

    def __init__(self, capacity: float, rate_per_sec: float):
        self.capacity = capacity
        self.bucket = TokenBucket(rate_per_sec, capacity)
        self.lock = threading.Lock()

    def consume(self, weight: float):
        while True:
            with self.lock:
                wait_time = self.bucket.consume(weight)
            if wait_time <= 0:
                return
            time.sleep(wait_time)


def _build_limiter() -> object:
    base_rate = _float_env("KRAKEN_RATE_PER_SEC_BASE", 20 / 3)
    min_rate = _float_env("KRAKEN_RATE_PER_SEC_MIN", 15 / 3)
    capacity = _float_env("KRAKEN_RATE_CAPACITY", 20.0)
    redis_url = os.getenv("KRAKEN_RATE_REDIS_URL")
    redis_prefix = os.getenv("KRAKEN_RATE_REDIS_PREFIX", "kraken_rate")
    if redis_url:
        try:
            return SharedRateLimiter(redis_url, base_rate, min_rate, capacity, key_prefix=redis_prefix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis limiter unavailable, falling back to local limiter: %s", exc)
    return LocalRateLimiter(base_rate, min_rate, capacity)


GLOBAL_RATE_LIMITER = _build_limiter()


ENDPOINT_WEIGHTS = {
    "public:Ticker": 1.0,
    "public:Depth": 2.0,
    "public:Trades": 1.0,
    "public:OHLC": 2.0,
    "private:AddOrder": 1.0,
    "private:Balance": 1.0,
    "private:OpenOrders": 1.0,
    "private:ClosedOrders": 1.0,
    "private:Ledgers": 2.0,
    "private:TradesHistory": 2.0,
    "private:ExportTrades": 5.0,
    "private:ExportOHLC": 5.0,
    "private:GetExport": 2.0,
}


class KrakenAdapter:
    _USER_LIMITERS: Dict[int, PerUserLimiter] = {}
    _USER_LIMITERS_LOCK = threading.Lock()

    def __init__(self, api_key: str, api_secret: str, rate_limiter: object | None = None, session: requests.Session | None = None, user_id: Optional[int] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.rate_limiter = rate_limiter or GLOBAL_RATE_LIMITER
        self.session = session or requests.Session()
        self.max_retries = int(os.getenv("KRAKEN_RETRIES", 3))
        self.backoff_base = _float_env("KRAKEN_BACKOFF_BASE", 0.5)
        self.user_id = user_id
        self.per_user_enabled = os.getenv("KRAKEN_PER_USER_BUCKET", "false").lower() in {"1", "true", "yes"}
        self.user_bucket_capacity = _float_env("KRAKEN_PER_USER_CAP", 10.0)
        self.user_bucket_rate = _float_env("KRAKEN_PER_USER_RATE", 5.0)

    def _rate_guard(self):
        self.rate_limiter.consume(1.0)

    def _sign(self, path: str, data: Dict[str, Any]) -> str:
        postdata = requests.compat.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = path.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _endpoint_weight(self, scope: str, endpoint: str, default: float = 1.0) -> float:
        return float(ENDPOINT_WEIGHTS.get(f"{scope}:{endpoint}", default))

    def limiter_snapshot(self) -> Dict[str, float]:
        snap = getattr(self.rate_limiter, "snapshot", None)
        if callable(snap):
            return snap()
        return {"mode": "unknown"}

    def _per_user_limiter(self) -> Optional[PerUserLimiter]:
        if not (self.per_user_enabled and self.user_id is not None):
            return None
        with self._USER_LIMITERS_LOCK:
            limiter = self._USER_LIMITERS.get(self.user_id)
            if limiter is None:
                limiter = PerUserLimiter(self.user_bucket_capacity, self.user_bucket_rate)
                self._USER_LIMITERS[self.user_id] = limiter
            return limiter

    def _private_request(self, endpoint: str, data: Dict[str, Any], retries: Optional[int] = None, backoff: Optional[float] = None, weight: Optional[float] = None) -> Dict[str, Any]:
        url = f"{PRIVATE_BASE}/{endpoint}"
        retries = retries if retries is not None else self.max_retries
        backoff = backoff if backoff is not None else self.backoff_base
        weight = weight if weight is not None else self._endpoint_weight("private", endpoint, 1.0)
        for attempt in range(retries):
            payload_data = dict(data)
            payload_data["nonce"] = int(time.time() * 1000)
            headers = {
                "API-Key": self.api_key,
                "API-Sign": self._sign(f"/0/private/{endpoint}", payload_data),
            }
            user_lim = self._per_user_limiter()
            if user_lim:
                user_lim.consume(weight)
            self.rate_limiter.consume(weight)
            resp = self.session.post(url, data=payload_data, headers=headers, timeout=10)
            try:
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("error"):
                    # Kraken returns list of error strings
                    error_list = payload.get("error")
                    if error_list:
                        err = ",".join(error_list)
                        if "Rate limit" in err:
                            self.rate_limiter.apply_penalty()
                            if attempt < retries - 1:
                                time.sleep(backoff * (2**attempt))
                                continue
                        raise RuntimeError(err)
                return payload
            except Exception:
                if attempt < retries - 1:
                    time.sleep(backoff * (2**attempt))
                    continue
                raise

    def _public_request(self, endpoint: str, params: Dict[str, Any] | None = None, retries: Optional[int] = None, backoff: Optional[float] = None, weight: Optional[float] = None) -> Dict[str, Any]:
        url = f"{PUBLIC_BASE}/{endpoint}"
        retries = retries if retries is not None else self.max_retries
        backoff = backoff if backoff is not None else self.backoff_base
        weight = weight if weight is not None else self._endpoint_weight("public", endpoint, 1.0)
        for attempt in range(retries):
            user_lim = self._per_user_limiter()
            if user_lim:
                user_lim.consume(weight)
            self.rate_limiter.consume(weight)
            resp = self.session.get(url, params=params or {}, timeout=10)
            try:
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("error"):
                    error_list = payload.get("error")
                    if error_list:
                        err = ",".join(error_list)
                        if "Rate limit" in err:
                            self.rate_limiter.apply_penalty()
                            if attempt < retries - 1:
                                time.sleep(backoff * (2**attempt))
                                continue
                        raise RuntimeError(err)
                return payload
            except Exception:
                if attempt < retries - 1:
                    time.sleep(backoff * (2**attempt))
                    continue
                raise

    def fetch_ticker(self, pair: str) -> Dict[str, Any]:
        payload = self._public_request("Ticker", params={"pair": pair})
        result = payload.get("result", {})
        first = next(iter(result.values()), {})
        price = None
        if first and "c" in first and first["c"]:
            price = float(first["c"][0])
        return {"pair": pair, "price": price, "raw": payload}

    def fetch_ohlc(self, pair: str, interval: int = 60, since: int | None = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pair": pair, "interval": interval}
        if since:
            params["since"] = since
        payload = self._public_request("OHLC", params=params)
        return payload

    def public_request(self, endpoint: str, params: Dict[str, Any] | None = None, weight: float | None = None) -> Dict[str, Any]:
        return self._public_request(endpoint, params=params or {}, weight=weight)

    def place_order(self, side: str, pair: str, volume: float, price: float | None = None, ordertype: str | None = None) -> Dict[str, Any]:
        ordertype = ordertype or ("limit" if price else "market")
        data = {
            "pair": pair,
            "type": side,
            "ordertype": ordertype,
            "volume": volume,
        }
        if price:
            data["price"] = price
        payload = self._private_request("AddOrder", data)
        return {
            "status": "submitted",
            "txid": payload.get("result", {}).get("txid"),
            "descr": payload.get("result", {}).get("descr"),
            "raw": payload,
        }

    def fetch_balances(self) -> Dict[str, float]:
        payload = self._private_request("Balance", {})
        result = payload.get("result", {})
        # returns dict asset: balance; keep as float
        return {asset: float(balance) for asset, balance in result.items()}
