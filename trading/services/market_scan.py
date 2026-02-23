import time
from typing import Any, Dict, List

from .kraken_adapter import GLOBAL_RATE_LIMITER, KrakenAdapter
from ..models import ProfitCandidatesCache
from .trainer import _compute_signals, _score_signals, _extract_ohlc  # type: ignore

_asset_pairs_cache: Dict[str, Any] = {"ts": 0.0, "pairs": []}
_candidates_cache: Dict[str, Any] = {"ts": 0.0, "quote": "", "items": []}


_PUBLIC_ADAPTER = KrakenAdapter("", "", rate_limiter=GLOBAL_RATE_LIMITER)


def _get_json(endpoint: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    # Use KrakenAdapter so all calls go through the shared rate limiter.
    weight = 1.0
    if endpoint == "OHLC":
        weight = 2.0
    elif endpoint in {"AssetPairs", "Assets"}:
        weight = 1.0
    elif endpoint == "Ticker":
        weight = 1.0
    return _PUBLIC_ADAPTER.public_request(endpoint, params=params or {}, weight=weight)


def _pairs_by_volume(pairs: List[str]) -> List[str]:
    volumes: Dict[str, float] = {}
    batch_size = 20
    for i in range(0, len(pairs), batch_size):
        batch = pairs[i : i + batch_size]
        if not batch:
            continue
        try:
            payload = _get_json("Ticker", params={"pair": ",".join(batch)})
            result = payload.get("result") or {}
            for pair_name, data in result.items():
                try:
                    v = (data or {}).get("v") or []
                    vol_24h = float(v[1]) if len(v) > 1 else float(v[0]) if v else 0.0
                except Exception:
                    vol_24h = 0.0
                volumes[pair_name] = vol_24h
        except Exception:
            continue

    # Keep original pair names; if Kraken returns different keys for a pair, we still
    # use the returned keys for sorting and intersection.
    return sorted(list(set(pairs)), key=lambda p: volumes.get(p, 0.0), reverse=True)


def _asset_pairs(quote: str) -> List[str]:
    now = time.time()
    if _asset_pairs_cache["pairs"] and (now - float(_asset_pairs_cache["ts"])) < 900:
        return list(_asset_pairs_cache["pairs"])

    data = _get_json("AssetPairs")
    result = data.get("result") or {}
    pairs: List[str] = []
    for pair_name, meta in result.items():
        if not isinstance(meta, dict):
            continue
        wsname = meta.get("wsname") or ""
        if not wsname or "/" not in wsname:
            continue
        base_ws, quote_ws = wsname.split("/", 1)
        if quote_ws.upper() != quote.upper():
            continue
        if meta.get("status") != "online":
            continue
        pairs.append(pair_name)

    _asset_pairs_cache["ts"] = now
    _asset_pairs_cache["pairs"] = pairs
    return pairs


def top_profit_candidates(limit: int = 20, quote: str = "USD", scan_pairs_limit: int = 40) -> List[Dict[str, Any]]:
    now = time.time()
    cached = get_cached_profit_candidates(limit=limit, quote=quote, max_age_seconds=300)
    if cached:
        return cached

    refresh_profit_candidates(limit=max(int(limit or 1), 1), quote=quote, scan_pairs_limit=scan_pairs_limit)
    items = list(_candidates_cache.get("items") or [])
    return items[: max(int(limit or 1), 1)]


def get_cached_profit_candidates(limit: int = 20, quote: str = "USD", max_age_seconds: int = 300) -> List[Dict[str, Any]]:
    now = time.time()
    # Fast path: in-process cache
    if (
        _candidates_cache.get("items")
        and _candidates_cache.get("quote") == quote.upper()
        and (now - float(_candidates_cache.get("ts") or 0.0)) < float(max_age_seconds or 0)
    ):
        items = list(_candidates_cache.get("items") or [])
        return items[: max(int(limit or 1), 1)]

    # Cross-process cache: DB
    row = ProfitCandidatesCache.objects.filter(quote=quote.upper()).order_by("-updated_at").first()
    if not row:
        return []
    age = now - float(row.updated_at.timestamp())
    if age > float(max_age_seconds or 0):
        return []
    items = list(row.items or [])
    # refresh local micro-cache
    _candidates_cache["ts"] = float(row.updated_at.timestamp())
    _candidates_cache["quote"] = quote.upper()
    _candidates_cache["items"] = items
    return items[: max(int(limit or 1), 1)]
    


def profit_candidates_last_updated() -> float:
    ts_local = float(_candidates_cache.get("ts") or 0.0)
    row = ProfitCandidatesCache.objects.order_by("-updated_at").first()
    ts_db = float(row.updated_at.timestamp()) if row else 0.0
    return max(ts_local, ts_db)


def refresh_profit_candidates(limit: int = 20, quote: str = "USD", scan_pairs_limit: int = 40) -> List[Dict[str, Any]]:
    now = time.time()
    all_pairs = _asset_pairs(quote)
    liquid_pairs = _pairs_by_volume(all_pairs)
    pairs = liquid_pairs[: max(int(scan_pairs_limit or 1), 1)]

    ranked: List[Dict[str, Any]] = []
    for pair in pairs:
        try:
            since = int(time.time() - 60 * 60 * 24 * 365)
            ohlc = _get_json("OHLC", params={"pair": pair, "interval": 60, "since": since})
            closes = _extract_ohlc(ohlc, pair)
            if len(closes) < 50:
                continue
            sig = _compute_signals(closes)
            if not sig:
                continue
            score = float(_score_signals(sig))
            ranked.append({"pair": pair, "score": score, "candles": len(closes)})
        except Exception:
            continue

    ranked.sort(key=lambda x: x["score"], reverse=True)
    _candidates_cache["ts"] = now
    _candidates_cache["quote"] = quote.upper()
    _candidates_cache["items"] = ranked
    ProfitCandidatesCache.objects.update_or_create(
        quote=quote.upper(),
        defaults={"items": ranked[: max(int(limit or 1), 1)]},
    )
    return ranked[: max(int(limit or 1), 1)]
