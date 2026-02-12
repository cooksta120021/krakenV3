from typing import Optional

from decimal import Decimal
from .models import AllocationHistory, ApiKey, CoreVault, Sleeve, PriceSnapshot, AuditLog


def select_api_key_for_owner(owner, validate_private: bool = False) -> Optional[ApiKey]:
    """Return next active API key for owner (single or round-robin).

    If validate_private is True, perform a private Kraken check and update health.
    """

    key = ApiKey.next_for_owner(owner)
    if key and validate_private:
        key.validate_private()
    return key


def allocate_to_sleeve(vault: CoreVault, sleeve: Sleeve, amount) -> bool:
    """Allocate funds to a sleeve if tradeable allows; records history and audit is expected upstream."""

    # if positions open, defer allocation change and log
    has_open = sleeve.orders.filter(status__in=["open", "filled"]).exists() if hasattr(sleeve, "orders") else False
    if has_open:
        AuditLog.objects.create(
            actor=vault.owner,
            action="allocation:block_open_positions",
            target_type="sleeve",
            target_id=str(sleeve.id),
            metadata={"requested": str(amount), "allocated": str(sleeve.allocated_amount)},
        )
        return False

    if not vault.can_allocate(amount - sleeve.allocated_amount):
        return False

    sleeve.allocated_amount = amount
    sleeve.save(update_fields=["allocated_amount"])
    AllocationHistory.objects.create(
        vault=vault,
        sleeve=sleeve,
        allocated_amount=amount,
        tradeable_remaining=vault.remaining_tradeable(),
        note="auto-allocation",
    )


def latest_snapshots_for_pairs(pairs):
    """Return latest snapshot per (asset, quote) pair."""

    latest = {}
    qs = (
        PriceSnapshot.objects.filter(
            asset__in=[p[0] for p in pairs], quote__in=[p[1] for p in pairs]
        )
        .order_by("asset", "quote", "-timestamp")
        .values("asset", "quote", "close", "timestamp")
    )
    for row in qs:
        key = (row["asset"], row["quote"])
        if key not in latest:
            latest[key] = row
    return latest


def log_price_snapshot(asset: str, quote: str, ohlcv: dict):
    """Persist a price snapshot (expects keys: open, high, low, close, volume, timestamp)."""

    PriceSnapshot.objects.create(
        asset=asset,
        quote=quote,
        open=Decimal(str(ohlcv.get("open", 0))),
        high=Decimal(str(ohlcv.get("high", 0))),
        low=Decimal(str(ohlcv.get("low", 0))),
        close=Decimal(str(ohlcv.get("close", 0))),
        volume=Decimal(str(ohlcv.get("volume", 0))),
        timestamp=ohlcv.get("timestamp"),
    )


def apply_trade_effects(vault: CoreVault, sleeve: Sleeve, side: str, amount: Decimal, profit: Decimal = Decimal("0")):
    """Adjust tradeable and sleeve profit tracking based on trade outcome."""

    if side == "buy":
        vault.tradeable_coins = max(Decimal("0"), vault.tradeable_coins - amount)
        vault.save(update_fields=["tradeable_coins"])
        if sleeve.sleeve_type == "flash" and sleeve.flash_principal <= 0:
            sleeve.flash_principal = amount
            sleeve.save(update_fields=["flash_principal"])
    elif side == "sell":
        vault.tradeable_coins = vault.tradeable_coins + amount
        vault.save(update_fields=["tradeable_coins"])

    if profit > 0:
        if sleeve.sleeve_type == "flash":
            vault.tradeable_coins = vault.tradeable_coins + profit
            vault.save(update_fields=["tradeable_coins"])
            sleeve.profit_returned = sleeve.profit_returned + profit
            sleeve.flash_principal = sleeve.allocated_amount  # reset baseline
            sleeve.save(update_fields=["profit_returned", "flash_principal"])
        else:
            sleeve.profit_retained = sleeve.profit_retained + profit
            sleeve.save(update_fields=["profit_retained"])
