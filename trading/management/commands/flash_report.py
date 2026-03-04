from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from trading.models import OrderLog, SleeveStrategy


class Command(BaseCommand):
    help = "Summarize flash-mode performance from OrderLog (realized pnl, win rate, trade count)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
        parser.add_argument("--user", type=int, default=0, help="Filter by user_id")
        parser.add_argument("--wallet", type=int, default=0, help="Filter by wallet_id")

    def handle(self, *args, **options):
        days = int(options.get("days") or 30)
        user_id = int(options.get("user") or 0)
        wallet_id = int(options.get("wallet") or 0)

        cutoff = timezone.now() - timedelta(days=max(days, 1))

        flash_sleeve_ids = set(
            SleeveStrategy.objects.filter(mode__icontains="flash")
            .values_list("sleeve_id", flat=True)
            .distinct()
        )
        if not flash_sleeve_ids:
            self.stdout.write("No flash sleeves found.")
            return

        q = OrderLog.objects.select_related("sleeve", "sleeve__wallet", "sleeve__wallet__user").filter(
            sleeve_id__in=flash_sleeve_ids,
            status="closed",
            created_at__gte=cutoff,
        )
        if user_id:
            q = q.filter(sleeve__wallet__user_id=user_id)
        if wallet_id:
            q = q.filter(sleeve__wallet_id=wallet_id)

        orders = list(q.only("sleeve_id", "created_at", "side", "amount", "price", "vol_exec", "cost", "fee", "base_asset", "quote_asset"))
        if not orders:
            self.stdout.write("No closed flash orders in window.")
            return

        # Per sleeve realized pnl using average-cost on closed trades.
        pos = defaultdict(lambda: Decimal("0"))
        cost = defaultdict(lambda: Decimal("0"))
        realized = defaultdict(lambda: Decimal("0"))
        wins = defaultdict(int)
        losses = defaultdict(int)
        last_side = defaultdict(str)
        trades = defaultdict(int)

        by_sleeve = defaultdict(list)
        for o in orders:
            by_sleeve[int(o.sleeve_id)].append(o)

        for sid, rows in by_sleeve.items():
            rows.sort(key=lambda r: r.created_at)
            for o in rows:
                side = str(o.side or "").lower()
                try:
                    qty = Decimal(str(getattr(o, "vol_exec", 0) or 0))
                except Exception:
                    qty = Decimal("0")
                if qty <= 0:
                    try:
                        qty = Decimal(str(getattr(o, "amount", 0) or 0))
                    except Exception:
                        qty = Decimal("0")

                try:
                    cost_quote = Decimal(str(getattr(o, "cost", 0) or 0))
                except Exception:
                    cost_quote = Decimal("0")
                try:
                    fee_quote = Decimal(str(getattr(o, "fee", 0) or 0))
                except Exception:
                    fee_quote = Decimal("0")
                try:
                    px = Decimal(str(getattr(o, "price", 0) or 0))
                except Exception:
                    px = Decimal("0")
                if cost_quote <= 0:
                    if qty <= 0 or px <= 0:
                        continue
                    cost_quote = (qty * px)
                if qty <= 0:
                    continue
                trades[sid] += 1

                avg = (cost[sid] / pos[sid]) if pos[sid] > 0 else Decimal("0")
                if side == "buy":
                    pos[sid] += qty
                    cost[sid] += (cost_quote + fee_quote)
                    last_side[sid] = "buy"
                elif side == "sell":
                    sell_qty = qty if qty <= pos[sid] else pos[sid]
                    if sell_qty > 0 and avg > 0:
                        proceeds = cost_quote
                        try:
                            proceeds = (cost_quote - fee_quote)
                        except Exception:
                            proceeds = cost_quote
                        pnl = (proceeds - (avg * sell_qty))
                        realized[sid] += pnl
                        if pnl >= 0:
                            wins[sid] += 1
                        else:
                            losses[sid] += 1
                        cost[sid] = max(Decimal("0"), cost[sid] - (avg * sell_qty))
                        pos[sid] = max(Decimal("0"), pos[sid] - sell_qty)
                    last_side[sid] = "sell"

        # Print report
        self.stdout.write(f"Flash report last {days}d (cutoff {cutoff.isoformat()})")
        self.stdout.write("sleeve_id | user | wallet | pair | trades | wins | losses | realized_pnl")

        total_pnl = Decimal("0")
        total_trades = 0
        for sid in sorted(by_sleeve.keys()):
            rows = by_sleeve[sid]
            first = rows[0]
            user = getattr(getattr(getattr(first, "sleeve", None), "wallet", None), "user", None)
            wallet = getattr(getattr(first, "sleeve", None), "wallet", None)
            u = getattr(user, "username", "?") if user else "?"
            wid = getattr(wallet, "id", "?") if wallet else "?"
            base = str(getattr(first, "base_asset", "") or "")
            quote = str(getattr(first, "quote_asset", "") or "")
            pair = f"{base}/{quote}" if base and quote else "?"
            r = realized[sid]
            total_pnl += r
            total_trades += int(trades[sid] or 0)

            self.stdout.write(
                f"{sid} | {u} | {wid} | {pair} | {trades[sid]} | {wins[sid]} | {losses[sid]} | {r.quantize(Decimal('0.00000001'))}"
            )

        self.stdout.write(f"TOTAL realized_pnl={total_pnl.quantize(Decimal('0.00000001'))} trades={total_trades}")
