"""Kraken worker scaffold (no live trading): fetch price, record snapshot, stub order flow."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from urllib.request import urlopen
import time
import random
import os

from .services import log_price_snapshot, select_api_key_for_owner, apply_trade_effects
from .models import CoreVault, Order, TradeFill, StrategyDecision, AuditLog, PriceSnapshot
from .utils import kraken_private_request


class KrakenWorker:
    def __init__(self, owner, asset: str, quote: str):
        self.owner = owner
        self.asset = asset
        self.quote = quote
        self.live_trading = os.getenv("KRAKEN_LIVE_TRADING", "false").lower() in {"1", "true", "yes"}
        self.paper_trading = os.getenv("PAPER_TRADING", "true").lower() in {"1", "true", "yes"}
        self.confidence_threshold = Decimal(os.getenv("TRADE_CONFIDENCE_MIN", "0.55"))
        self.expected_return_min = Decimal(os.getenv("TRADE_EXPECTED_RETURN_MIN", "0"))
        self.trade_cooldown_sec = float(os.getenv("TRADE_COOLDOWN_SEC", "1.5"))
        self.trailing_exit_pct = Decimal(os.getenv("TRAILING_EXIT_PCT", "0.02"))
        self.volatility_max = Decimal(os.getenv("VOLATILITY_MAX", "0"))  # 0 disables
        self.sweep_cadence_sec = float(os.getenv("SWEEP_CADENCE_SEC", "0"))
        self.trade_windows = os.getenv("TRADE_WINDOWS", "")  # e.g., "00:00-23:59"
        if not hasattr(KrakenWorker, "last_decision_at"):
            KrakenWorker.last_decision_at = {}
        if not hasattr(KrakenWorker, "last_sweep_at"):
            KrakenWorker.last_sweep_at = {}
        if not hasattr(KrakenWorker, "high_water"):
            KrakenWorker.high_water = {}
        if not hasattr(KrakenWorker, "market_cache"):
            KrakenWorker.market_cache = {}

    def place_order_live(self, key, secret, side: str, volume: Decimal):
        pair = f"{self.asset}{self.quote}"
        payload = {
            "pair": pair,
            "type": side,
            "ordertype": "market",
            "volume": str(volume),
        }
        return kraken_private_request(key, secret, path="/0/private/AddOrder", data=payload)

    def fetch_balance_live(self, key, secret):
        return kraken_private_request(key, secret, path="/0/private/Balance")

    def fetch_24h_change(self):
        pair = f"{self.asset}{self.quote}"
        cache = KrakenWorker.market_cache.get(pair)
        if cache and (time.time() - cache.get("ts", 0)) < 900:
            return cache.get("change", Decimal("0")), cache.get("ohlc", [])
        # OHLC interval 60 minutes, ~24 points for 24h
        try:
            with urlopen(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=60", timeout=5) as resp:
                data = json.loads(resp.read().decode())
                if data.get("error"):
                    raise Exception(";".join(data.get("error")))
                ohlc = list(data.get("result", {}).values())[0]
                closes = [Decimal(str(c[4])) for c in ohlc[-24:]] if len(ohlc) >= 2 else []
                if len(closes) >= 2:
                    change = (closes[-1] - closes[0]) / closes[0]
                else:
                    change = Decimal("0")
                KrakenWorker.market_cache[pair] = {"ts": time.time(), "change": change, "ohlc": closes}
                return change, closes
        except Exception:
            return Decimal("0"), []
        return Decimal("0"), []

    def generate_ml_signals(self, sleeve):
        # directional/outcome/regressor stubs from recent price snapshots
        change24, closes = self.fetch_24h_change()
        if closes:
            rets = []
            for i in range(1, len(closes)):
                prev = closes[i - 1]
                curr = closes[i]
                if prev > 0:
                    rets.append((curr - prev) / prev)
            mean_ret = sum(rets) / len(rets) if rets else Decimal("0")
            stdev_ret = Decimal(str((sum((float(r - mean_ret) ** 2 for r in rets)) / len(rets)) ** 0.5)) if rets else Decimal("0")
        else:
            mean_ret = Decimal(os.getenv("ML_EXPECTED_RETURN", "0"))
            stdev_ret = Decimal("0")
        directional_prob = float(1 / (1 + pow(2.71828, -float(mean_ret * 100))))
        conf = Decimal(str(directional_prob))
        exp_ret = Decimal(str(mean_ret))
        regime = "bull" if change24 > 0 else "bear" if change24 < 0 else "neutral"

        # derive per-sleeve script likelihoods
        quiet_buy = max(Decimal("0"), exp_ret - stdev_ret) * conf
        quiet_sell = max(Decimal("0"), stdev_ret - exp_ret)
        flash_buy = max(Decimal("0"), exp_ret * Decimal("1.5")) * conf
        flash_sell = max(Decimal("0"), stdev_ret + (Decimal("0.5") * conf))

        sleeve.confidence = conf
        sleeve.expected_return = exp_ret
        sleeve.regime = regime
        sleeve.save(update_fields=["confidence", "expected_return", "regime"])
        StrategyDecision.objects.create(
            sleeve=sleeve,
            action="ml_signals",
            confidence=conf,
            metadata={
                "expected_return": str(exp_ret),
                "regime": regime,
                "stdev": str(stdev_ret),
                "quiet_buy": str(quiet_buy),
                "quiet_sell": str(quiet_sell),
                "flash_buy": str(flash_buy),
                "flash_sell": str(flash_sell),
            },
        )
        return {
            "quiet_buy": quiet_buy,
            "quiet_sell": quiet_sell,
            "flash_buy": flash_buy,
            "flash_sell": flash_sell,
        }

    def fetch_ticker(self):
        pair = f"{self.asset}{self.quote}"
        with urlopen(f"https://api.kraken.com/0/public/Ticker?pair={pair}", timeout=5) as resp:
            data = json.loads(resp.read().decode())
            if data.get("error"):
                raise Exception(";".join(data.get("error")))
            result = list(data.get("result", {}).values())[0]
            ask = Decimal(result["a"][0])
            bid = Decimal(result["b"][0])
            return (ask + bid) / 2

    def fetch_ticker_with_retry(self, attempts=3, delay=0.3):
        last_exc = None
        for _ in range(attempts):
            try:
                return self.fetch_ticker()
            except Exception as exc:
                last_exc = exc
                time.sleep(delay)
        if last_exc:
            raise last_exc

    def record_snapshot(self, price: Decimal):
        now = datetime.now(timezone.utc)
        log_price_snapshot(
            self.asset,
            self.quote,
            {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": Decimal("0"),
                "timestamp": now,
            },
        )

    def run_once(self):
        # Select vaults for owner matching asset/quote
        vaults = CoreVault.objects.filter(owner=self.owner, asset=self.asset, quote_asset=self.quote, paused=False)
        if not vaults:
            return

        if self.paper_trading:
            self.live_trading = False
        api_key = select_api_key_for_owner(self.owner, validate_private=not self.paper_trading)
        if not api_key and not self.paper_trading:
            StrategyDecision.objects.create(sleeve=None, action="health:no_api_key", confidence=None, metadata={"pair": f"{self.asset}{self.quote}"})
            return

        price = self.fetch_ticker_with_retry()
        self.record_snapshot(price)
        StrategyDecision.objects.create(sleeve=None, action="loop_tick", confidence=None, metadata={"pair": f"{self.asset}{self.quote}"})

        # Stub: record a no-op order for telemetry
        decisions = []
        for vault in vaults:
            # auto-raise totals if tradeable+allocations exceed stored total (simulated funding detection)
            inferred_total = vault.tradeable_coins + vault.quiet_allocation + vault.flash_allocation
            if inferred_total > vault.total_coins:
                vault.apply_funding(inferred_total)
                AuditLog.objects.create(actor=self.owner, action="vault:funding_auto", target_type="corevault", target_id=str(vault.id), metadata={"inferred_total": str(inferred_total)})
                StrategyDecision.objects.create(
                    sleeve=vault.sleeves.first() if vault.sleeves.exists() else None,
                    action="funding_detected",
                    confidence=None,
                    metadata={"inferred_total": str(inferred_total)},
                )
            # enforce quote constraint and allowed pairs
            if vault.quote_asset != self.quote:
                decisions.append((vault.id, "quote_mismatch"))
                StrategyDecision.objects.create(
                    sleeve=vault.sleeves.first() if vault.sleeves.exists() else None,
                    action="quote_blocked",
                    confidence=None,
                    metadata={"expected_quote": vault.quote_asset, "worker_quote": self.quote},
                )
                AuditLog.objects.create(actor=self.owner, action="quote:block", target_type="corevault", target_id=str(vault.id), metadata={"expected_quote": vault.quote_asset, "worker_quote": self.quote})
                continue
            if vault.allowed_pairs and f"{self.asset}{self.quote}" not in vault.allowed_pairs:
                decisions.append((vault.id, "pair_not_allowed"))
                StrategyDecision.objects.create(
                    sleeve=vault.sleeves.first() if vault.sleeves.exists() else None,
                    action="pair_blocked",
                    confidence=None,
                    metadata={"pair": f"{self.asset}{self.quote}"},
                )
                AuditLog.objects.create(actor=self.owner, action="pair:block", target_type="corevault", target_id=str(vault.id), metadata={"pair": f"{self.asset}{self.quote}"})
                continue

            # pause/start/stop using BotRun status if exists
            botrun = vault.sleeves.first().bot_runs.last() if vault.sleeves.exists() else None
            if botrun and botrun.status != "running":
                decisions.append((vault.id, botrun.status))
                continue

            if vault.remaining_tradeable() <= 0:
                decisions.append((vault.id, "no_tradeable"))
                StrategyDecision.objects.create(
                    sleeve=vault.sleeves.first() if vault.sleeves.exists() else None,
                    action="no_tradeable",
                    confidence=None,
                    metadata={"tradeable": str(vault.tradeable_coins)},
                )
                if not vault.allow_zero_tradeable:
                    AuditLog.objects.create(actor=self.owner, action="vault:tradeable_block", target_type="corevault", target_id=str(vault.id), metadata={"tradeable": str(vault.tradeable_coins)})
                continue
            sleeve = vault.sleeves.first()
            if not sleeve:
                decisions.append((vault.id, "no_sleeve"))
                continue
            self.generate_ml_signals(sleeve)
            # decision log stub (would include signals and constraints)
            remaining = vault.remaining_tradeable()
            if remaining < 0:
                # normalize tradeable to allocations to avoid over-trading
                vault.tradeable_coins = vault.quiet_allocation + vault.flash_allocation
                vault.save(update_fields=["tradeable_coins"])
                StrategyDecision.objects.create(
                    sleeve=sleeve,
                    action="normalize_tradeable",
                    confidence=None,
                    metadata={"remaining": str(remaining)},
                )
                remaining = Decimal("0")
            # trade window filter
            if self.trade_windows:
                window_ok = False
                now_str = datetime.utcnow().strftime("%H:%M")
                for w in self.trade_windows.split(","):
                    if "-" in w:
                        start, end = w.split("-")
                        if start <= now_str <= end:
                            window_ok = True
                            break
                if not window_ok:
                    StrategyDecision.objects.create(sleeve=sleeve, action="window_hold", confidence=None, metadata={"remaining": str(remaining)})
                    decision = "hold"
                    size = Decimal("0")
                    continue

            # volatility filter placeholder (uses slip_pct as proxy)
            if self.volatility_max > 0 and slip_pct > self.volatility_max:
                StrategyDecision.objects.create(sleeve=sleeve, action="volatility_hold", confidence=None, metadata={"slip_pct": str(slip_pct)})
                decision = "hold"
                size = Decimal("0")
                continue

            # trailing exit guard: track high watermark, exit to cash if drawdown exceeds threshold
            last_key = f"{vault.id}:{sleeve.id}"
            hw = KrakenWorker.high_water.get(last_key, price)
            if price > hw:
                KrakenWorker.high_water[last_key] = price
            drawdown = (hw - price) / hw if hw > 0 else Decimal("0")
            if self.trailing_exit_pct > 0 and drawdown >= self.trailing_exit_pct:
                StrategyDecision.objects.create(sleeve=sleeve, action="trailing_exit", confidence=None, metadata={"hw": str(hw), "price": str(price), "drawdown": str(drawdown)})
                decisions.append((vault.id, "trailing_exit"))
                apply_trade_effects(vault, sleeve, "sell", size if size > 0 else Decimal("0"), profit=Decimal("0"))
                continue

            # throttle/cooldown
            last_ts = KrakenWorker.last_decision_at.get(last_key)
            if last_ts and (time.time() - last_ts) < self.trade_cooldown_sec:
                StrategyDecision.objects.create(sleeve=sleeve, action="cooldown_hold", confidence=None, metadata={"remaining": str(remaining)})
                decision = "hold"
            else:
                # mode-aware decision with expected_return/confidence gates
                if sleeve.mode == "ml":
                    conf = sleeve.confidence or Decimal("0")
                    exp_ret = sleeve.expected_return or Decimal("0")
                    if conf >= self.confidence_threshold and exp_ret >= self.expected_return_min and remaining > 0:
                        decision = "buy"
                        # size scale with expected return
                        size = min(remaining, Decimal("0.1")) * (Decimal("1") + exp_ret)
                    else:
                        decision = "hold"
                        StrategyDecision.objects.create(
                            sleeve=sleeve,
                            action="mode_ml_fallback",
                            confidence=None,
                            metadata={"remaining": str(remaining), "conf": str(conf), "exp_ret": str(exp_ret)},
                        )
                else:
                    decision = "buy" if remaining > 0 else "hold"
                KrakenWorker.last_sweep_at[last_key] = KrakenWorker.last_sweep_at.get(last_key, 0)
                KrakenWorker.last_decision_at[last_key] = time.time()
            size = size if 'size' in locals() else (min(remaining, Decimal("0.1")) if remaining > 0 else Decimal("0"))
            fee_rate = vault.fee_rate or Decimal("0.0")
            slip_pct = vault.slippage_pct or Decimal("0.0")
            fee = size * fee_rate
            slip_price = price * (Decimal("1.0") + slip_pct)

            StrategyDecision.objects.create(
                sleeve=sleeve,
                action=decision,
                confidence=None,
                metadata={"price": str(price), "tradeable": str(remaining)},
            )

            if decision != "buy":
                decisions.append((vault.id, decision))
                continue

            if size <= 0:
                decisions.append((vault.id, "no_tradeable"))
                continue

            if self.live_trading:
                # live buy
                key, secret = api_key.get_secret()
                try:
                    resp_buy = self.place_order_live(key, secret, "buy", size)
                    txid_buy = resp_buy.get("result", {}).get("txid", [None])[0]
                except Exception as exc:
                    StrategyDecision.objects.create(sleeve=sleeve, action="buy_error", confidence=None, metadata={"error": str(exc)})
                    AuditLog.objects.create(actor=self.owner, action="order:error", target_type="corevault", target_id=str(vault.id), metadata={"pair": f"{self.asset}{self.quote}", "error": str(exc)})
                    time.sleep(1.0)
                    continue
                order = Order.objects.create(
                    sleeve=sleeve,
                    side="buy",
                    amount=size,
                    price=slip_price,
                    status="filled",
                    exchange_order_id=txid_buy or "kraken_buy",
                    fees=fee,
                    filled_amount=size,
                )
                TradeFill.objects.create(order=order, price=slip_price, amount=size, fee=fee)
                apply_trade_effects(vault, sleeve, "buy", size, profit=Decimal("0"))

                # live sell flash sweep
                try:
                    resp_sell = self.place_order_live(key, secret, "sell", size)
                    txid_sell = resp_sell.get("result", {}).get("txid", [None])[0]
                except Exception as exc:
                    StrategyDecision.objects.create(sleeve=sleeve, action="sell_error", confidence=None, metadata={"error": str(exc)})
                    AuditLog.objects.create(actor=self.owner, action="order:error", target_type="corevault", target_id=str(vault.id), metadata={"pair": f"{self.asset}{self.quote}", "error": str(exc)})
                    time.sleep(1.0)
                    continue
                exit_price = price * (Decimal("1.0") + slip_pct + Decimal("0.002"))
                sell_fee = size * fee_rate
                TradeFill.objects.create(order=order, price=exit_price, amount=size * -1, fee=sell_fee)
                proceeds = (size * exit_price) - sell_fee
                cost = (size * slip_price) + fee
                sell_profit = proceeds - cost
                if sell_profit < 0:
                    sell_profit = Decimal("0")
                apply_trade_effects(vault, sleeve, "sell", size, profit=sell_profit)
                StrategyDecision.objects.create(
                    sleeve=sleeve,
                    action="flash_sweep_live",
                    confidence=None,
                    metadata={"profit": str(sell_profit), "price": str(exit_price), "txid": txid_sell},
                )
                order.note = f"profit_sweep={sell_profit}" if hasattr(order, "note") else None
                order.save(update_fields=["note"] if hasattr(order, "note") else [])
            else:
                order = Order.objects.create(
                    sleeve=sleeve,
                    side="buy",
                    amount=size,
                    price=slip_price,
                    status="filled",
                    exchange_order_id="simulated",
                    fees=fee,
                    filled_amount=size,
                )
                TradeFill.objects.create(order=order, price=slip_price, amount=size, fee=fee)
                decisions.append((vault.id, "buy_filled"))

                apply_trade_effects(vault, sleeve, "buy", size, profit=Decimal("0"))

                # Sell path with flash sweep profit return based on fills
                last_sweep = KrakenWorker.last_sweep_at.get(last_key, 0)
                if self.sweep_cadence_sec > 0 and (time.time() - last_sweep) < self.sweep_cadence_sec:
                    StrategyDecision.objects.create(sleeve=sleeve, action="sweep_throttled", confidence=None, metadata={"cadence_sec": self.sweep_cadence_sec})
                    continue
                exit_price = price * (Decimal("1.0") + slip_pct + Decimal("0.002"))
                sell_fee = size * fee_rate
                sell_fill = TradeFill.objects.create(order=order, price=exit_price, amount=size * -1, fee=sell_fee)

                proceeds = (size * exit_price) - sell_fee
                cost = (size * slip_price) + fee
                sell_profit = proceeds - cost
                if sell_profit < 0:
                    sell_profit = Decimal("0")

                apply_trade_effects(vault, sleeve, "sell", size, profit=sell_profit)
                StrategyDecision.objects.create(
                    sleeve=sleeve,
                    action="flash_sweep",
                    confidence=None,
                    metadata={"profit": str(sell_profit), "price": str(exit_price), "cadence": "on_sell"},
                )
                order.note = f"profit_sweep={sell_profit}" if hasattr(order, "note") else None
                order.save(update_fields=["note"] if hasattr(order, "note") else [])
                KrakenWorker.last_sweep_at[last_key] = time.time()

        # rate-limit friendly pause placeholder
        time.sleep(0.2 + random.random() * 0.1)


def run_all(owner):
    # Iterate unique (asset, quote) pairs and run worker once each (headless loop stub)
    pairs = CoreVault.objects.filter(owner=owner, paused=False).values_list("asset", "quote_asset").distinct()
    for asset, quote in pairs:
        worker = KrakenWorker(owner, asset, quote)
        try:
            worker.run_once()
        except Exception as exc:
            AuditLog.objects.create(actor=owner, action="worker:error", target_type="worker", target_id=f"{asset}{quote}", metadata={"error": str(exc)})
            StrategyDecision.objects.create(sleeve=None, action="worker_error", confidence=None, metadata={"pair": f"{asset}{quote}", "error": str(exc)})
            time.sleep(1.0)
            continue
        time.sleep(0.2)
        # funding poll per vault for alerts
        for vault in CoreVault.objects.filter(owner=owner, asset=asset, quote_asset=quote, paused=False):
            inferred_total = vault.tradeable_coins + vault.quiet_allocation + vault.flash_allocation
            if inferred_total > vault.total_coins:
                vault.apply_funding(inferred_total)
                AuditLog.objects.create(actor=owner, action="vault:funding_poll", target_type="corevault", target_id=str(vault.id), metadata={"inferred_total": str(inferred_total)})
                StrategyDecision.objects.create(sleeve=vault.sleeves.first() if vault.sleeves.exists() else None, action="funding_poll", confidence=None, metadata={"inferred_total": str(inferred_total)})


def run_continuous(owner, iterations=5, sleep_s=2):
    """Headless loop stub: repeatedly run all active pairs with spacing."""

    for _ in range(iterations):
        run_all(owner)
        time.sleep(sleep_s)
