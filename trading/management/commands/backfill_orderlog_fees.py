from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Dict, List

from django.core.management.base import BaseCommand
from django.utils import timezone

from trading.models import OrderLog
from trading.services.kraken_adapter import GLOBAL_RATE_LIMITER, KrakenAdapter


class Command(BaseCommand):
    help = "Backfill OrderLog vol_exec/cost/fee from Kraken QueryOrders for historical txids."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=0, help="Only backfill orders created in last N days (0 = all)")
        parser.add_argument("--limit", type=int, default=2000, help="Max OrderLog rows to process (default: 2000)")
        parser.add_argument("--batch", type=int, default=20, help="Txid batch size per Kraken call (default: 20)")
        parser.add_argument("--dry-run", action="store_true", help="Print what would change without saving")

    def handle(self, *args, **options):
        days = int(options.get("days") or 0)
        limit = max(int(options.get("limit") or 2000), 1)
        batch = max(min(int(options.get("batch") or 20), 50), 1)
        dry_run = bool(options.get("dry_run"))

        qs = (
            OrderLog.objects.select_related("api_key", "sleeve", "sleeve__wallet")
            .exclude(txid="")
            .filter(status="closed")
            .filter(
                # missing execution fields
                cost=0,
            )
            .order_by("-created_at")
        )
        # Also include rows where fee/vol_exec missing even if cost is filled.
        qs2 = (
            OrderLog.objects.select_related("api_key", "sleeve", "sleeve__wallet")
            .exclude(txid="")
            .filter(status="closed")
            .filter(fee=0)
            .order_by("-created_at")
        )
        qs3 = (
            OrderLog.objects.select_related("api_key", "sleeve", "sleeve__wallet")
            .exclude(txid="")
            .filter(status="closed")
            .filter(vol_exec=0)
            .order_by("-created_at")
        )

        cutoff = None
        if days and days > 0:
            cutoff = timezone.now() - timedelta(days=days)
            qs = qs.filter(created_at__gte=cutoff)
            qs2 = qs2.filter(created_at__gte=cutoff)
            qs3 = qs3.filter(created_at__gte=cutoff)

        # Merge candidates (cheap in python; limit afterwards)
        cand_map: Dict[int, OrderLog] = {}
        for o in list(qs[:limit]) + list(qs2[:limit]) + list(qs3[:limit]):
            cand_map[int(o.id)] = o
        orders = list(cand_map.values())
        orders.sort(key=lambda r: r.created_at, reverse=True)
        orders = orders[:limit]

        if not orders:
            self.stdout.write("No OrderLog rows need backfill.")
            return

        grouped: Dict[int, List[OrderLog]] = defaultdict(list)
        for o in orders:
            try:
                grouped[int(o.api_key_id)].append(o)
            except Exception:
                continue

        checked = 0
        updated = 0
        calls = 0

        for api_key_id, rows in grouped.items():
            key = rows[0].api_key
            if not key:
                continue
            try:
                user_id = int(rows[0].sleeve.wallet.user_id)
            except Exception:
                user_id = None

            adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=user_id)

            # chunk txids
            by_txid: Dict[str, OrderLog] = {}
            for r in rows:
                t = str(getattr(r, "txid", "") or "").strip()
                if t:
                    by_txid[t] = r
            txids = list(by_txid.keys())
            for i in range(0, len(txids), batch):
                chunk = txids[i : i + batch]
                if not chunk:
                    continue
                try:
                    payload = adapter._private_request("QueryOrders", {"txid": ",".join(chunk)}, weight=1.0)
                    calls += 1
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(self.style.WARNING(f"QueryOrders failed for api_key_id={api_key_id}: {exc}"))
                    continue

                result = payload.get("result") or {}
                if not isinstance(result, dict):
                    continue

                for txid, meta in result.items():
                    checked += 1
                    if not isinstance(meta, dict):
                        continue
                    match = by_txid.get(str(txid))
                    if not match:
                        continue

                    try:
                        vol_exec = Decimal(str(meta.get("vol_exec") or 0))
                    except Exception:
                        vol_exec = Decimal("0")
                    try:
                        cost = Decimal(str(meta.get("cost") or 0))
                    except Exception:
                        cost = Decimal("0")
                    try:
                        fee = Decimal(str(meta.get("fee") or 0))
                    except Exception:
                        fee = Decimal("0")

                    avg_price = Decimal("0")
                    if vol_exec > 0 and cost > 0:
                        try:
                            avg_price = (cost / vol_exec).quantize(Decimal("0.0000000001"))
                        except Exception:
                            avg_price = Decimal("0")

                    update_fields: List[str] = []
                    if vol_exec >= 0 and match.vol_exec != vol_exec:
                        match.vol_exec = vol_exec
                        update_fields.append("vol_exec")
                    if cost >= 0 and match.cost != cost:
                        match.cost = cost
                        update_fields.append("cost")
                    if fee >= 0 and match.fee != fee:
                        match.fee = fee
                        update_fields.append("fee")
                    if avg_price > 0 and match.price != avg_price:
                        match.price = avg_price
                        update_fields.append("price")

                    if update_fields:
                        if dry_run:
                            self.stdout.write(f"DRY order_id={match.id} txid={txid} set {update_fields}")
                        else:
                            try:
                                match.save(update_fields=update_fields)
                                updated += 1
                            except Exception as exc:  # noqa: BLE001
                                self.stdout.write(self.style.WARNING(f"Save failed order_id={match.id} txid={txid}: {exc}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill complete checked={checked} updated={updated} calls={calls} dry_run={dry_run}"
            )
        )
