import logging

from django.db.models import Q

from trading.models import MlCandle, SleeveStrategy

logger = logging.getLogger(__name__)


def cleanup_orphan_ml_candles() -> dict[str, int]:
    """Delete MlCandle rows for pairs with no active ML strategies.

    A pair is retained if any SleeveStrategy is ml_active=True for that pair (even if paused).
    """
    # Candle stores are keyed by (pair, interval_minutes). Flash uses 15m, quiet uses 60m.
    active_pairs_intervals: set[tuple[str, int]] = set()
    try:
        active_qs = (
            SleeveStrategy.objects.filter(ml_active=True)
            .exclude(Q(ml_pair__isnull=True) | Q(ml_pair=""))
            .values_list("ml_pair", "mode")
        )
        for pair, mode in active_qs:
            try:
                m = str(mode or "").lower()
            except Exception:
                m = ""
            interval = 15 if "flash" in m else 60
            active_pairs_intervals.add((str(pair), int(interval)))
    except Exception:
        active_pairs_intervals = set()

    all_pairs_intervals = set(MlCandle.objects.values_list("pair", "interval_minutes").distinct())
    orphan_pairs_intervals = sorted(pi for pi in all_pairs_intervals if pi not in active_pairs_intervals)

    deleted = 0
    for pair, interval_minutes in orphan_pairs_intervals:
        try:
            d, _ = MlCandle.objects.filter(pair=pair, interval_minutes=int(interval_minutes)).delete()
            deleted += int(d or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MlCandle cleanup failed for %s/%sm: %s", pair, interval_minutes, exc)

    return {
        "pairs_total": len(all_pairs_intervals),
        "pairs_active": len(active_pairs_intervals),
        "pairs_orphan": len(orphan_pairs_intervals),
        "rows_deleted": deleted,
    }
