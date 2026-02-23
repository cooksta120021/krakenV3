import time
from typing import Any

from django.core.cache import cache


def _key(user_id: int) -> str:
    return f"autotrade_console:{int(user_id)}"


def push(user_id: int, msg: str, *, level: str = "info", max_lines: int = 200) -> None:
    try:
        uid = int(user_id)
    except Exception:
        return

    m = (msg or "").strip()
    if not m:
        return

    line = {"ts": time.time(), "level": (level or "info").strip(), "msg": m}

    k = _key(uid)
    try:
        cur = cache.get(k) or []
    except Exception:
        cur = []

    if not isinstance(cur, list):
        cur = []

    try:
        if cur and isinstance(cur[-1], dict):
            last_msg = str(cur[-1].get("msg") or "")
            last_level = str(cur[-1].get("level") or "")
            last_ts = float(cur[-1].get("ts") or 0.0)
            if last_msg == m and last_level == (level or "info").strip() and (time.time() - last_ts) < 2.0:
                return
    except Exception:
        pass

    cur.append(line)
    if len(cur) > max_lines:
        cur = cur[-max_lines:]

    try:
        cache.set(k, cur, timeout=60 * 60 * 24)
    except Exception:
        return


def clear(user_id: int) -> None:
    try:
        uid = int(user_id)
    except Exception:
        return
    try:
        cache.delete(_key(uid))
    except Exception:
        return


def tail(user_id: int, *, limit: int = 80) -> list[dict[str, Any]]:
    try:
        uid = int(user_id)
    except Exception:
        return []

    k = _key(uid)
    try:
        cur = cache.get(k) or []
    except Exception:
        cur = []

    if not isinstance(cur, list):
        return []

    # Hide noisy heartbeat-style lines so the console focuses on actionable events.
    # These can remain in cache (historical), but the UI shouldn't display them.
    try:
        cur = [
            x
            for x in cur
            if isinstance(x, dict)
            and isinstance(x.get("msg"), str)
            and not (
                x.get("msg", "").startswith("ml_price_tick_run")
                or x.get("msg", "").startswith("tick sleeve=")
            )
        ]
    except Exception:
        pass

    try:
        lim = max(int(limit or 0), 0)
    except Exception:
        lim = 0

    if lim <= 0:
        return list(cur)

    return list(cur[-lim:])
