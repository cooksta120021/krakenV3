from __future__ import annotations

from decimal import Decimal
from typing import Dict

from api_keys.models import ApiKey
from trading.services.kraken_adapter import GLOBAL_RATE_LIMITER, KrakenAdapter
from wallets.models import Sleeve, Wallet


def _normalize_balance_map(raw: Dict[str, float]) -> Dict[str, Decimal]:
    balance_map: Dict[str, Decimal] = {}
    for asset, val in (raw or {}).items():
        val_dec = Decimal(str(val))
        balance_map[asset] = val_dec
        balance_map[asset.upper()] = val_dec
        if asset.startswith("Z") or asset.startswith("X"):
            balance_map[asset[1:]] = val_dec
            balance_map[asset[1:].upper()] = val_dec
        if asset.startswith("XX") or asset.startswith("ZZ"):
            balance_map[asset[1:]] = val_dec
            balance_map[asset[1:].upper()] = val_dec
    return balance_map


def refresh_wallet_balances() -> Dict[str, int]:
    updated_wallets = 0
    users = (
        Wallet.objects.values_list("user_id", flat=True)
        .distinct()
    )

    for user_id in users:
        key = ApiKey.objects.filter(user_id=user_id, is_active=True).order_by("created_at").first()
        if not key:
            continue

        adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=user_id)
        raw_bal = adapter.fetch_balances()
        bal = _normalize_balance_map(raw_bal)

        wallets = list(Wallet.objects.filter(user_id=user_id))
        wallet_ids = [w.id for w in wallets]
        allocated: Dict[int, Decimal] = {wid: Decimal("0") for wid in wallet_ids}
        sleeves = Sleeve.objects.filter(wallet_id__in=wallet_ids)
        for s in sleeves:
            allocated[s.wallet_id] = allocated.get(s.wallet_id, Decimal("0")) + (s.allocated_balance or Decimal("0"))

        for w in wallets:
            real_val = bal.get(w.currency) or bal.get(w.currency.upper())
            if real_val is None:
                continue
            tradeable = max(Decimal("0"), real_val - allocated.get(w.id, Decimal("0")))
            if w.real_balance != real_val or w.tradeable_balance != tradeable:
                w.real_balance = real_val
                w.tradeable_balance = tradeable
                w.save(update_fields=["real_balance", "tradeable_balance"])
                updated_wallets += 1

    return {"updated_wallets": updated_wallets}
