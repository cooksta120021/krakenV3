from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.db import IntegrityError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST
from django.conf import settings
import os

from accounts.views import require_role, role_required
from .forms import ApiKeyForm, CoreVaultForm, WalletForm
from .models import ApiKey, AuditLog, CoreVault, SleevePnl, VaultPnl, Wallet, Order, TradeFill, PriceSnapshot, Sleeve, BotRun, StrategyDecision
from .services import allocate_to_sleeve, latest_snapshots_for_pairs
from .utils import kraken_private_request

@login_required
@role_required({"admin", "mod", "member"})
def dashboard(request):
    vaults = CoreVault.objects.filter(owner=request.user).prefetch_related("sleeves")
    api_keys = ApiKey.objects.filter(owner=request.user)
    vault_pnls = VaultPnl.latest_for_vaults([v.id for v in vaults])
    sleeve_pnls = SleevePnl.latest_for_sleeves([s.id for v in vaults for s in v.sleeves.all()])
    pairs = [(v.asset, v.quote_asset) for v in vaults]
    latest_prices = latest_snapshots_for_pairs(pairs) if pairs else {}
    # per-sleeve pnl series for charts
    sleeve_ids = [s.id for v in vaults for s in v.sleeves.all()]
    pnl_series = {}
    pnl_snaps = SleevePnl.objects.filter(sleeve_id__in=sleeve_ids).order_by("sleeve_id", "created_at") if sleeve_ids else []
    for snap in pnl_snaps:
        pnl_series.setdefault(snap.sleeve_id, []).append(
            {"x": snap.created_at.isoformat(), "y": float(snap.unrealized + snap.realized)}
        )
    # recent orders/fills for positions/trades view
    recent_orders = (
        Order.objects.filter(sleeve__vault__owner=request.user)
        .select_related("sleeve", "sleeve__vault")
        .order_by("-created_at")[:25]
    )
    recent_fills = (
        TradeFill.objects.filter(order__sleeve__vault__owner=request.user)
        .select_related("order", "order__sleeve", "order__sleeve__vault")
        .order_by("-created_at")[:25]
    )
    recent_decisions = (
        StrategyDecision.objects.filter(sleeve__vault__owner=request.user)
        .select_related("sleeve", "sleeve__vault")
        .order_by("-created_at")[:20]
    )

    # chart datasets: per pair last 20 snapshots
    chart_datasets = []
    if pairs:
        for asset, quote in pairs:
            snaps = (
                PriceSnapshot.objects.filter(asset=asset, quote=quote)
                .order_by("-timestamp")[:20]
            )[::-1]
            data = [{"x": s.timestamp.isoformat(), "y": float(s.close)} for s in snaps]
            if data:
                chart_datasets.append({"label": f"{asset}/{quote}", "data": data})

    for v in vaults:
        v.latest_pnl = vault_pnls.get(v.id)
        v.latest_price = latest_prices.get((v.asset, v.quote_asset))
        for s in v.sleeves.all():
            s.latest_pnl = sleeve_pnls.get(s.id)
    return render(
        request,
        "dashboard.html",
        {
            "vaults": vaults,
            "api_keys": api_keys,
            "recent_orders": recent_orders,
            "recent_fills": recent_fills,
            "recent_decisions": recent_decisions,
            "chart_datasets": chart_datasets,
            "pnl_series": pnl_series,
            "paper_trading": getattr(settings, "PAPER_TRADING_UI", True),
        },
    )


@login_required
@role_required({"admin", "mod", "member"})
def positions(request):
    sleeves = (
        Sleeve.objects.filter(vault__owner=request.user)
        .select_related("vault")
        .prefetch_related("orders", "orders__fills")
    )
    sleeve_ids = [s.id for s in sleeves]
    sleeve_pnls = SleevePnl.latest_for_sleeves(sleeve_ids) if sleeve_ids else {}
    pairs = list({(s.vault.asset, s.vault.quote_asset) for s in sleeves})
    latest_prices = latest_snapshots_for_pairs(pairs) if pairs else {}
    # chart data map per pair
    chart_map = {}
    for asset, quote in pairs:
        snaps = (
            PriceSnapshot.objects.filter(asset=asset, quote=quote)
            .order_by("-timestamp")[:30]
        )[::-1]
        chart_map[f"{asset}{quote}"] = [
            {"x": s.timestamp.isoformat(), "y": float(s.close)} for s in snaps
        ]

    # per-sleeve pnl series
    pnl_map = {}
    pnl_snaps = SleevePnl.objects.filter(sleeve_id__in=sleeve_ids).order_by("sleeve_id", "created_at")
    for snap in pnl_snaps:
        pnl_map.setdefault(snap.sleeve_id, []).append(
            {
                "x": snap.created_at.isoformat(),
                "y": float(snap.unrealized + snap.realized),
            }
        )
    for s in sleeves:
        s.latest_pnl = sleeve_pnls.get(s.id)
        fills = [f for o in s.orders.all() for f in o.fills.all()]
        net_qty = sum((f.amount if f.order.side == "buy" else -f.amount) for f in fills) if fills else 0
        cost_basis = sum((f.amount * f.price) for f in fills if f.order.side == "buy") if fills else 0
        s.total_fees = sum((f.fee for f in fills), start=0) if fills else 0
        avg_price = (cost_basis / net_qty) if fills and net_qty else 0
        price_snap = latest_prices.get((s.vault.asset, s.vault.quote_asset))
        last_price = price_snap.get("close") if price_snap else None
        unrealized = (net_qty * last_price) - cost_basis if last_price is not None and net_qty else 0
        s.net_qty = net_qty
        s.avg_price = avg_price
        s.unrealized_position = unrealized
    # live balances/positions from Kraken using first api key
    live_balances = None
    live_positions = None
    api_key = ApiKey.objects.filter(owner=request.user, is_active=True).first()
    if api_key:
        try:
            key, secret = api_key.get_secret()
            live_balances = kraken_private_request(key, secret, path="/0/private/Balance")
            live_positions = kraken_private_request(key, secret, path="/0/private/OpenPositions")
        except Exception as exc:
            live_balances = {"error": str(exc)}
            live_positions = {"error": str(exc)}
    return render(request, "positions.html", {"sleeves": sleeves, "chart_map": chart_map, "pnl_map": pnl_map, "live_balances": live_balances, "live_positions": live_positions, "paper_trading": getattr(settings, "PAPER_TRADING_UI", True)})


@login_required
@role_required({"admin", "mod", "member"})
def wallets(request):
    wallets_qs = Wallet.objects.filter(owner=request.user)
    message = None
    if request.method == "POST":
        edit_id = request.POST.get("id")
        instance = wallets_qs.filter(id=edit_id).first() if edit_id else None
        form = WalletForm(request.POST, instance=instance)
        if form.is_valid():
            wallet = form.save(commit=False)
            wallet.owner = request.user
            try:
                wallet.save()
                AuditLog.objects.create(actor=request.user, action="wallet:upsert", target_type="wallet", target_id=str(wallet.id), metadata={"asset": wallet.asset, "name": wallet.name})
                return redirect("wallets")
            except IntegrityError:
                message = "A wallet with that name already exists."
    else:
        form = WalletForm()
    return render(
        request,
        "wallets.html",
        {"form": form, "wallets": wallets_qs, "message": message, "paper_trading": getattr(settings, "PAPER_TRADING_UI", True)},
    )


@login_required
@role_required({"admin", "mod", "member"})
def vault_list(request):
    vaults_qs = CoreVault.objects.filter(owner=request.user).prefetch_related("sleeves")
    vault_pnls = VaultPnl.latest_for_vaults([v.id for v in vaults_qs])
    sleeve_pnls = SleevePnl.latest_for_sleeves([s.id for v in vaults_qs for s in v.sleeves.all()])
    for v in vaults_qs:
        v.latest_pnl = vault_pnls.get(v.id)
        for s in v.sleeves.all():
            s.latest_pnl = sleeve_pnls.get(s.id)
    message = None
    if request.method == "POST":
        edit_id = request.POST.get("id")
        instance = vaults_qs.filter(id=edit_id).first() if edit_id else None
        form = CoreVaultForm(request.POST, user=request.user, instance=instance)
        if form.is_valid():
            vault = form.save(commit=False)
            vault.owner = request.user
            vault.save()
            # enforce allocations within tradeable via helper
            for sleeve in vault.sleeves.all():
                allocate_to_sleeve(vault, sleeve, sleeve.allocated_amount)
            AuditLog.objects.create(
                actor=request.user,
                action="vault:upsert",
                target_type="corevault",
                target_id=str(vault.id),
                metadata={
                    "asset": vault.asset,
                    "quote": vault.quote_asset,
                    "tradeable": str(vault.tradeable_coins),
                    "quiet": str(vault.quiet_allocation),
                    "flash": str(vault.flash_allocation),
                },
            )
            return redirect("vault_list")
    else:
        form = CoreVaultForm(user=request.user)
    return render(
        request,
        "vaults.html",
        {"form": form, "vaults": vaults_qs, "message": message, "paper_trading": getattr(settings, "PAPER_TRADING_UI", True)},
    )


@login_required
@role_required({"admin", "mod", "member"})
def api_keys(request):
    keys_qs = ApiKey.objects.filter(owner=request.user)
    if request.method == "POST":
        edit_id = request.POST.get("id")
        instance = keys_qs.filter(id=edit_id).first() if edit_id else None
        form = ApiKeyForm(request.POST, instance=instance)
        if form.is_valid():
            api_key = form.save(commit=False)
            api_key.owner = request.user
            api_key.exchange = "kraken"
            if api_key.rotation_index is None:
                api_key.rotation_index = 0
            if api_key.health_status in [None, ""]:
                api_key.health_status = "unknown"
            api_key.is_active = True
            if form.cleaned_data.get("key_plain") and form.cleaned_data.get("secret_plain"):
                api_key.set_secret(
                    form.cleaned_data.get("key_plain"),
                    form.cleaned_data.get("secret_plain"),
                )
            api_key.save()
            api_key.validate_keys()
            api_key.validate_private()
            AuditLog.objects.create(actor=request.user, action="apikey:upsert", target_type="apikey", target_id=str(api_key.id), metadata={"exchange": api_key.exchange, "label": api_key.label, "health": api_key.health_status})
            return redirect("api_keys")
    else:
        form = ApiKeyForm()
    return render(
        request,
        "api_keys.html",
        {"form": form, "api_keys": keys_qs, "paper_trading": getattr(settings, "PAPER_TRADING_UI", True)},
    )


@login_required
@role_required({"admin", "mod"})
def toggle_paper_trading(request):
    mode = request.POST.get("mode")
    if mode == "live":
        os.environ["PAPER_TRADING"] = "false"
        settings.PAPER_TRADING_UI = False
    else:
        os.environ["PAPER_TRADING"] = "true"
        settings.PAPER_TRADING_UI = True
    return redirect(request.META.get("HTTP_REFERER", "/vaults/"))


@login_required
@require_POST
def rotate_api_key(request):
    if not require_role(request.user, {"admin", "mod", "member"}):
        return HttpResponseForbidden()
    vault_id = request.POST.get("vault_id")
    key = ApiKey.next_for_owner(request.user)
    if key:
        key.validate_private()
        AuditLog.objects.create(actor=request.user, action="apikey:rotate", target_type="apikey", target_id=str(key.id))
    return redirect("vault_list")


@login_required
@require_POST
@role_required({"admin", "mod", "member"})
def delete_wallet(request, wallet_id):
    Wallet.objects.filter(owner=request.user, id=wallet_id).delete()
    AuditLog.objects.create(actor=request.user, action="wallet:delete", target_type="wallet", target_id=str(wallet_id))
    return redirect("wallets")


@login_required
@require_POST
@role_required({"admin", "mod", "member"})
def delete_vault(request, vault_id):
    CoreVault.objects.filter(owner=request.user, id=vault_id).delete()
    AuditLog.objects.create(actor=request.user, action="vault:delete", target_type="corevault", target_id=str(vault_id))
    return redirect("vault_list")


@login_required
@require_POST
@role_required({"admin", "mod", "member"})
def delete_api_key(request, api_key_id):
    ApiKey.objects.filter(owner=request.user, id=api_key_id).delete()
    AuditLog.objects.create(actor=request.user, action="apikey:delete", target_type="apikey", target_id=str(api_key_id))
    return redirect("api_keys")


@login_required
@role_required({"admin"})
def admin_dashboard(request):
    audit_logs = AuditLog.objects.filter(actor=request.user).order_by("-created_at")[:50]
    decisions = (
        StrategyDecision.objects.filter(sleeve__vault__owner=request.user)
        .select_related("sleeve", "sleeve__vault")
        .order_by("-created_at")[:50]
    )
    return render(request, "admin_dashboard.html", {"audit_logs": audit_logs, "decisions": decisions})


@login_required
@require_POST
def pause_vault(request, vault_id):
    if not require_role(request.user, {"admin", "mod", "member"}):
        return HttpResponseForbidden()
    vault = CoreVault.objects.filter(owner=request.user, id=vault_id).first()
    if vault:
        vault.paused = True
        vault.save(update_fields=["paused"])
        AuditLog.objects.create(actor=request.user, action="vault:pause", target_type="corevault", target_id=str(vault_id))
    return redirect("vault_list")


@login_required
@require_POST
def resume_vault(request, vault_id):
    if not require_role(request.user, {"admin", "mod", "member"}):
        return HttpResponseForbidden()
    vault = CoreVault.objects.filter(owner=request.user, id=vault_id).first()
    if vault:
        vault.paused = False
        vault.save(update_fields=["paused"])
        AuditLog.objects.create(actor=request.user, action="vault:resume", target_type="corevault", target_id=str(vault_id))
    return redirect("vault_list")


@login_required
@require_POST
def start_bot(request, vault_id):
    if not require_role(request.user, {"admin", "mod", "member"}):
        return HttpResponseForbidden()
    vault = CoreVault.objects.filter(owner=request.user, id=vault_id).first()
    if vault and vault.sleeves.exists():
        sleeve = vault.sleeves.first()
        BotRun.objects.create(sleeve=sleeve, started_by=request.user, status="running")
        AuditLog.objects.create(actor=request.user, action="bot:start", target_type="corevault", target_id=str(vault_id))
    return redirect("vault_list")


@login_required
@require_POST
def stop_bot(request, vault_id):
    if not require_role(request.user, {"admin", "mod", "member"}):
        return HttpResponseForbidden()
    vault = CoreVault.objects.filter(owner=request.user, id=vault_id).first()
    if vault and vault.sleeves.exists():
        sleeve = vault.sleeves.first()
        BotRun.objects.create(sleeve=sleeve, started_by=request.user, stopped_by=request.user, status="stopped")
        AuditLog.objects.create(actor=request.user, action="bot:stop", target_type="corevault", target_id=str(vault_id))
    return redirect("vault_list")


@login_required
@require_POST
def validate_api_key(request, api_key_id):
    if not require_role(request.user, {"admin", "mod", "member"}):
        return HttpResponseForbidden()
    api_key = ApiKey.objects.filter(owner=request.user, id=api_key_id).first()
    if api_key:
        api_key.validate_keys()
        api_key.validate_private()
        AuditLog.objects.create(
            actor=request.user,
            action="apikey:validate",
            target_type="apikey",
            target_id=str(api_key_id),
            metadata={"health": api_key.health_status, "error": api_key.last_error},
        )
    return redirect("api_keys")
