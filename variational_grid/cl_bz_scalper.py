"""Paper CL long scalping with a barrel-for-barrel BZ short hedge.

Only indicative quotes are requested. Orders below are local simulation intents,
not venue orders. Each new two-sided RFQ observation can fill one batch only.
"""
from contextlib import closing
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import sqlite3
import time

from .cl_bz_market import CommodityClient, fresh, market_gate
from .comparison import Cohort, write_json
from .models import D, GridError, Quote, dec, utc, validate_pair
from .qqq_scalper import cooldown_seconds
from .store import Store


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)


@dataclass
class ScalperConfig:
    state_file: str
    quantity_barrels: str = "1"
    max_batches: int = 30
    take_profit_percent: str = "0.1"
    wait_seconds: int = 450
    reprice_after_seconds: int = 20
    reprice_poll_seconds: int = 5
    initial_balance_usdc: str = "1000"
    paper_leverage: str = "5"
    max_margin_fraction: str = "0.80"
    max_drawdown_fraction: str = "0.20"
    fee_bps: str = "0"
    slippage_bps: str = "1"
    poll_seconds: int = 10
    max_quote_age_seconds: int = 15
    max_pair_skew_seconds: int = 5

    def validate(self):
        for key in ("quantity_barrels", "initial_balance_usdc", "paper_leverage"):
            if not 0 < dec(getattr(self, key)) <= 1000000:
                raise GridError("Invalid CL scalper size or paper capital")
        if not 0 < dec(self.take_profit_percent) < 100:
            raise GridError("Invalid CL take profit percent")
        for key in ("max_margin_fraction", "max_drawdown_fraction"):
            if not 0 < dec(getattr(self, key)) <= 1:
                raise GridError("Invalid CL scalper risk fraction")
        for key in ("fee_bps", "slippage_bps"):
            if not 0 <= dec(getattr(self, key)) < 1000:
                raise GridError("Invalid CL scalper costs")
        if type(self.max_batches) is not int or not 1 <= self.max_batches <= 30:
            raise GridError("CL scalper supports 1–30 batches")
        for key in ("wait_seconds", "reprice_after_seconds", "reprice_poll_seconds", "poll_seconds", "max_quote_age_seconds", "max_pair_skew_seconds"):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= 3600:
                raise GridError("Invalid CL scalper timing")
        if self.poll_seconds < 5 or self.max_quote_age_seconds > 60:
            raise GridError("CL polling must be at least 5 seconds; quotes expire within 60 seconds")
        return self

    def strategy_identity(self):
        return encoded({"model": "cl_scalper_bz_equal_barrels_v1", **self.parameters()})

    def parameters(self):
        return {key: value for key, value in asdict(self).items() if key != "state_file"}


@dataclass
class ScalperExperiment:
    base: object
    output: Path
    scenarios: dict
    source_paths: tuple
    kind = "cl_bz_scalper"

    @classmethod
    def from_data(cls, path, data):
        from .cli import configuration
        try:
            path = Path(path).resolve()
            if set(data) != {"kind", "base_config", "output_dir", "strategy"} or data["kind"] != cls.kind:
                raise ValueError()
            base_path = (path.parent / data["base_config"]).resolve()
            base = configuration(base_path)
            output = (path.parent / data["output_dir"]).resolve()
            protected = (path, base_path, Path(base.session_file).resolve(), Path(base.state_file).resolve())
            if any(output.is_relative_to(p) or p.is_relative_to(output) for p in protected):
                raise GridError("CL scalper output must be separate from configuration, session and ledger")
            config = ScalperConfig(state_file=str(output / "ledgers/cl-long-bz-hedge.sqlite3"), **data["strategy"]).validate()
            return cls(base, output, {"cl-long-bz-hedge": config}, protected)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise GridError("Invalid CL scalper experiment; require a separate output and strategy settings") from None

    @property
    def settings(self):
        return next(iter(self.scenarios.values()))

    def identity(self):
        return {"kind": self.kind, "version": 1,
                "scenarios": {name: json.loads(config.strategy_identity()) for name, config in self.scenarios.items()}}


def validate_companion(primary, companion):
    if getattr(primary, "kind", None) != "qqq_hedge" or getattr(companion, "kind", None) != "cl_bz_scalper":
        raise GridError("CL scalper companion requires a QQQ primary experiment")
    roots = [primary.output, getattr(primary, "previous_output", None)]
    for path in filter(None, roots):
        if companion.output.is_relative_to(path) or path.is_relative_to(companion.output):
            raise GridError("CL scalper output must be separate from QQQ data")
        if any(p.is_relative_to(path) for p in companion.source_paths):
            raise GridError("CL scalper configuration must be separate from QQQ data")
    if Path(primary.base.session_file).resolve() != Path(companion.base.session_file).resolve():
        raise GridError("Companion and QQQ must share the same Var session file")


@dataclass
class ScalperFrame:
    ts: float
    markets: dict
    quotes: dict
    reason: str | None = None
    data_kind: str = "live_indicative"

    def validate(self, experiment):
        if not fresh(self.ts, self.ts, 1) or self.data_kind not in {"live_indicative", "synthetic"}:
            raise GridError("Invalid CL scalper frame")
        if not isinstance(self.markets, dict) or set(self.markets) != {"CL", "BZ"}:
            raise GridError("Missing CL/BZ market state")
        for row in self.markets.values():
            if not isinstance(row, dict) or row.get("state") not in {"open", "closed", "close_only", "unknown"}:
                raise GridError("Invalid CL/BZ market state")
        market_gate(self.markets, self.ts)
        if self.reason is not None and (not isinstance(self.reason, str) or len(self.reason) > 300):
            raise GridError("Invalid CL/BZ frame reason")
        if self.quotes:
            if set(self.quotes) != {"CL", "BZ"}:
                raise GridError("Both RFQ legs required")
            config = experiment.settings
            for symbol, row in self.quotes.items():
                if set(row) != {"bid", "ask", "mark", "qty", "ts"}:
                    raise GridError("Invalid CL/BZ quote shape")
                bid, ask, mark, qty = (dec(row[key]) for key in ("bid", "ask", "mark", "qty"))
                if bid <= 0 or ask < bid or mark <= 0 or qty != dec(config.quantity_barrels) or not fresh(row["ts"], row["ts"], 1):
                    raise GridError("Invalid CL/BZ quote")

    def encode(self):
        return encoded({"version": 1, **asdict(self)})

    @classmethod
    def decode(cls, raw):
        data = json.loads(raw)
        if data.pop("version", None) != 1:
            raise GridError("Unknown CL scalper frame version")
        return cls(**data)


def initial_account():
    def leg():
        return {"qty": "0", "average_entry": "0", "realized_gross": "0", "fees_usdc": "0", "volume_units": "0", "turnover_usdc": "0", "fill_count": 0}
    return {"cl": leg(), "bz": leg(), "slots": [], "orders": [], "closed_batches": [],
            "next_order": 1, "next_batch": 1, "next_fill": 1, "last_entry_ts": None, "decision_count": 0,
            "closed_count": 0, "last_consumed": {"CL": 0, "BZ": 0}, "last_quotes": {},
            "pair_pause": {"active": False, "resume_after_ts": 0}, "peak_equity": None,
            "max_drawdown": "0", "max_margin": "0", "halted": False, "segment": 0}


class ScalperStore:
    get, set, transaction, snapshot, close = Store.get, Store.set, Store.transaction, Store.snapshot, Store.close

    def __init__(self, path, config):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS ticks(id INTEGER PRIMARY KEY,ts REAL UNIQUE,snapshot TEXT NOT NULL,account TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,frame_ts REAL NOT NULL,payload TEXT NOT NULL);")
        identity = self.get("config")
        if identity is not None and identity != config.strategy_identity():
            self.close()
            raise GridError("CL scalper settings changed; select a new output_dir")
        with self.transaction():
            self.set("config", config.strategy_identity())
            if self.get("account") is None:
                self.set("account", encoded(initial_account()))

    def account(self):
        return json.loads(self.get("account"))

    def reset(self, config):
        with self.transaction():
            for table in ("ticks", "fills", "meta"):
                self.db.execute("DELETE FROM " + table)
            self.set("config", config.strategy_identity())
            self.set("account", encoded(initial_account()))


def book_fill(account, symbol, change, price, config, ts, slot, entry=None):
    """Realize the batch's actual cost, including when batches exit out of order."""
    leg = account[symbol.lower()]
    old, avg = dec(leg["qty"]), dec(leg["average_entry"])
    new = old + change
    realized = D(0) if entry is None else abs(change) * (price - dec(entry)) * (1 if old > 0 else -1)
    cost = abs(old) * avg + abs(change) * price if entry is None else abs(old) * avg - abs(change) * dec(entry)
    fee = abs(change) * price * dec(config.fee_bps) / 10000
    leg.update(qty=str(new), average_entry=str(cost / abs(new) if new else D(0)),
               realized_gross=str(dec(leg["realized_gross"]) + realized), fees_usdc=str(dec(leg["fees_usdc"]) + fee),
               volume_units=str(dec(leg["volume_units"]) + abs(change)), turnover_usdc=str(dec(leg["turnover_usdc"]) + abs(change) * price),
               fill_count=leg["fill_count"] + 1)
    result = {"id": account["next_fill"], "ts": ts, "venue": "Variational", "symbol": symbol,
              "side": "buy" if change > 0 else "sell", "qty": str(abs(change)), "price": str(price),
              "fee": str(fee), "slot": slot, "reason": "cl_take_profit" if entry is not None else "cl_entry"}
    account["next_fill"] += 1
    return result


def valuation(account, config):
    quotes = account["last_quotes"]
    legs, margin = {}, D(0)
    for symbol in ("CL", "BZ"):
        leg = account[symbol.lower()]
        qty = dec(leg["qty"])
        mark = dec(quotes[symbol]["mark"]) if quotes else D(0)
        unrealized = floating_pnl(symbol, qty, dec(leg["average_entry"]), quotes, config) if qty else D(0)
        realized = dec(leg["realized_gross"]) - dec(leg["fees_usdc"])
        legs[symbol.lower()] = {**leg, "unrealized_pnl_usdc": str(unrealized), "realized_pnl_usdc": str(realized), "total_pnl_usdc": str(realized + unrealized)}
        margin += abs(qty) * mark / dec(config.paper_leverage)
    total = sum(dec(leg["total_pnl_usdc"]) for leg in legs.values())
    return legs, total, dec(config.initial_balance_usdc) + total, margin


def floating_pnl(symbol, qty, entry, quotes, config):
    slip = dec(config.slippage_bps) / 10000
    price = dec(quotes[symbol]["bid" if qty > 0 else "ask"]) * (1 - slip if qty > 0 else 1 + slip)
    return qty * (price - entry) - abs(qty) * price * dec(config.fee_bps) / 10000


class ScalperEngine:
    def __init__(self, config, store):
        self.config, self.store = config, store

    def calculate(self, frame):
        account, config, fills = self.store.account(), self.config, []
        now, quotes = frame.ts, frame.quotes
        reason, barrier = market_gate(frame.markets, now)
        quote_ready = bool(quotes) and all(fresh(q["ts"], now, config.max_quote_age_seconds) for q in quotes.values())
        quote_ready = quote_ready and abs(quotes["CL"]["ts"] - quotes["BZ"]["ts"]) <= config.max_pair_skew_seconds
        if quote_ready and account["last_quotes"]:
            quote_ready = all(quotes[s]["ts"] >= account["last_quotes"][s]["ts"] for s in ("CL", "BZ"))
        reason = reason or frame.reason or (None if quote_ready else "CL / BZ 报价缺失、过期或时间不同步，暂停模拟订单")
        pause = account["pair_pause"]
        resumed = False
        if reason:
            if not pause["active"]:
                account["segment"] += 1
            pause.update(active=True, reason=reason, resume_after_ts=max(pause.get("resume_after_ts", 0), barrier or now), markets=frame.markets)
            account["orders"] = []
        elif pause["active"]:
            if all(q["ts"] > pause["resume_after_ts"] for q in quotes.values()):
                pause.update(active=False, reason=None, markets=frame.markets)
                account["segment"] += 1
                resumed = True
            else:
                reason = "等待休市/暂停屏障之后的双腿新报价"
                pause.update(reason=reason, markets=frame.markets)
        else:
            pause["markets"] = frame.markets
        # Retain the old source timestamp across pauses. Never mark an old price as current.
        if quote_ready and not reason:
            account["last_quotes"] = quotes
        legs, total, equity, margin = valuation(account, config)
        peak = max(dec(account["peak_equity"] or config.initial_balance_usdc), equity)
        drawdown = max(D(0), peak - equity)
        account["peak_equity"] = str(peak)
        account["max_drawdown"] = str(max(dec(account["max_drawdown"]), drawdown))
        if equity <= 0 or drawdown >= dec(config.initial_balance_usdc) * dec(config.max_drawdown_fraction):
            account["halted"] = True
        slip = dec(config.slippage_bps) / 10000
        quantity = dec(config.quantity_barrels)

        def prices(opening):
            return (dec(quotes["CL"]["ask" if opening else "bid"]) * (1 + slip if opening else 1 - slip),
                    dec(quotes["BZ"]["bid" if opening else "ask"]) * (1 - slip if opening else 1 + slip))

        def affordable():
            cl, bz = prices(True)
            reserve = quantity * (cl + bz) / dec(config.paper_leverage)
            fee = quantity * (cl + bz) * dec(config.fee_bps) / 10000
            return equity > fee and margin + reserve <= (equity - fee) * dec(config.max_margin_fraction)

        if not reason:
            if account["halted"] or not affordable():
                account["orders"] = [order for order in account["orders"] if order["side"] == "sell"]
            if not resumed:
                for order in sorted(account["orders"], key=lambda row: (row["side"] != "sell", dec(row["price"]), row["id"])):
                    if not all(quotes[s]["ts"] > max(order["sources"][s], account["last_consumed"][s]) for s in ("CL", "BZ")):
                        continue
                    opening = order["side"] == "buy"
                    cl, bz = prices(opening)
                    if opening and (cl > dec(order["price"]) or account["halted"] or not affordable()):
                        continue
                    if not opening and cl < dec(order["price"]):
                        continue
                    if opening:
                        slot_id = account["next_batch"]
                        account["next_batch"] += 1
                        slot = {"id": slot_id, "qty": str(quantity), "entry_cl": str(cl), "entry_bz": str(bz),
                                "opened": now, "tp_price": str(cl * (1 + dec(config.take_profit_percent) / 100))}
                        a = book_fill(account, "CL", quantity, cl, config, now, slot_id)
                        b = book_fill(account, "BZ", -quantity, bz, config, now, slot_id)
                        slot.update(cl_entry_fee=a["fee"], bz_entry_fee=b["fee"])
                        account["slots"].append(slot)
                        account["last_entry_ts"] = now
                    else:
                        slot = next(row for row in account["slots"] if row["id"] == order["slot"])
                        a = book_fill(account, "CL", -quantity, cl, config, now, slot["id"], slot["entry_cl"])
                        b = book_fill(account, "BZ", quantity, bz, config, now, slot["id"], slot["entry_bz"])
                        cl_pnl = quantity * (cl - dec(slot["entry_cl"])) - dec(a["fee"]) - dec(slot["cl_entry_fee"])
                        bz_pnl = quantity * (dec(slot["entry_bz"]) - bz) - dec(b["fee"]) - dec(slot["bz_entry_fee"])
                        account["closed_batches"] = (account["closed_batches"] + [{**slot, "closed": now, "exit_cl": str(cl), "exit_bz": str(bz),
                            "cl_pnl": str(cl_pnl), "bz_pnl": str(bz_pnl), "net_pnl": str(cl_pnl + bz_pnl), "reason": "cl_take_profit"}])[-100:]
                        account["slots"].remove(slot)
                        account["closed_count"] += 1
                    fills.extend((a, b))
                    account["orders"].remove(order)
                    account["last_consumed"] = {s: quotes[s]["ts"] for s in ("CL", "BZ")}
                    # One one-barrel RFQ observation cannot fill multiple batches.
                    break

        def create_order(side, price, slot=None):
            account["orders"].append({"id": account["next_order"], "slot": slot, "side": side, "price": str(price),
                "qty": str(quantity), "submitted_ts": now, "checked_ts": now, "sources": {s: quotes[s]["ts"] for s in ("CL", "BZ")}})
            account["next_order"] += 1

        count = len(account["slots"])
        cooldown = cooldown_seconds(count, config.max_batches, config.wait_seconds)
        remaining = max(0, (account["last_entry_ts"] or 0) + cooldown - now) if account["last_entry_ts"] is not None else 0
        if count < account["decision_count"]:
            remaining = 0
        candidate = None
        phase = "paused" if reason else "ready"
        if not reason:
            for slot in account["slots"]:
                if not any(o["side"] == "sell" and o["slot"] == slot["id"] for o in account["orders"]):
                    create_order("sell", slot["tp_price"], slot["id"])
            candidate = min([(dec(quotes["CL"]["bid"]) + dec(quotes["CL"]["ask"])) / 2,
                             *(dec(slot["tp_price"]) for slot in account["slots"])])
            legs, total, equity, margin = valuation(account, config)
            entry = next((o for o in account["orders"] if o["side"] == "buy"), None)
            can_open = not account["halted"] and count < config.max_batches and affordable()
            if entry is not None and not can_open:
                account["orders"].remove(entry)
                entry = None
            if can_open and entry is None and not remaining:
                create_order("buy", candidate)
            elif can_open and entry is not None and now - entry["submitted_ts"] >= config.reprice_after_seconds and now - entry["checked_ts"] >= config.reprice_poll_seconds:
                entry["checked_ts"] = now
                if candidate > dec(entry["price"]):
                    account["orders"].remove(entry)
                    create_order("buy", candidate)
            phase = "halted" if account["halted"] else "capacity" if count >= config.max_batches else "margin" if not affordable() else "cooldown" if remaining else "ready"
            account["decision_count"] = count
        legs, total, equity, margin = valuation(account, config)
        account["max_margin"] = str(max(dec(account["max_margin"]), margin))
        peak = max(dec(account["peak_equity"]), equity)
        account["peak_equity"] = str(peak)
        account["max_drawdown"] = str(max(dec(account["max_drawdown"]), peak - equity))
        if equity <= 0 or peak - equity >= dec(config.initial_balance_usdc) * dec(config.max_drawdown_fraction):
            account["halted"] = True
            account["orders"] = [o for o in account["orders"] if o["side"] == "sell"]
            if not reason:
                phase = "halted"
        if dec(account["cl"]["qty"]) != quantity * count or dec(account["bz"]["qty"]) != -quantity * count or count > config.max_batches:
            raise GridError("CL/BZ simulated inventory invariant failed")
        entries = sum(o["side"] == "buy" for o in account["orders"])
        targets = sum(o["side"] == "sell" for o in account["orders"])
        if entries > 1 or count + entries > config.max_batches or (not reason and targets != count):
            raise GridError("CL scalper order coverage invariant failed")
        realized = sum(dec(leg["realized_pnl_usdc"]) for leg in legs.values())
        row = {**legs, "take_profit_percent": config.take_profit_percent, "quantity_barrels": config.quantity_barrels,
               "initial_balance_usdc": config.initial_balance_usdc, "cl_barrels": legs["cl"]["qty"], "bz_barrels": legs["bz"]["qty"],
               "open_pairs": count, "opened_pairs": account["next_batch"] - 1, "closed_pairs": account["closed_count"],
               "total_pnl_usdc": str(total), "realized_pnl_usdc": str(realized), "unrealized_pnl_usdc": str(total - realized),
               "return_fraction": str(total / dec(config.initial_balance_usdc)), "equity_usdc": str(equity), "margin_usdc": str(margin),
               "margin_limit_usdc": str(max(D(0), equity) * dec(config.max_margin_fraction)),
               "position_notional_usdc": str(margin * dec(config.paper_leverage)),
               "halted": "达到本轮回撤上限" if account["halted"] else None,
               "open_allowed": not reason and not account["halted"] and count < config.max_batches and phase != "margin",
               "skip_reason": reason or phase,
               "max_margin_usdc": account["max_margin"], "max_drawdown_usdc": account["max_drawdown"],
               "max_drawdown_fraction": str(dec(account["max_drawdown"]) / dec(config.initial_balance_usdc)),
               "fees_usdc": str(sum(dec(leg["fees_usdc"]) for leg in legs.values())),
               "turnover_usdc": str(sum(dec(leg["turnover_usdc"]) for leg in legs.values())),
               "volume_barrels": str(sum(dec(leg["volume_units"]) for leg in legs.values())), "fill_count": account["next_fill"] - 1,
               "scalper": {"phase": phase, "active_entries": entries, "active_take_profits": targets, "occupied_batches": count + entries,
                   "max_batches": config.max_batches, "cooldown_seconds": cooldown, "cooldown_remaining_seconds": remaining,
                   "next_entry_at": now + remaining, "candidate_entry_price": str(candidate) if candidate is not None else None,
                   "candidate_tp_price": str(candidate * (1 + dec(config.take_profit_percent) / 100)) if candidate is not None else None}}
        return account, row, fills


class ScalperCohort(Cohort):
    def create_store(self, config):
        return ScalperStore(config.state_file, config)

    def create_engine(self, config, store):
        return ScalperEngine(config, store)

    def decode_frame(self, raw):
        frame = ScalperFrame.decode(raw)
        frame.validate(self.experiment)
        return frame

    def apply(self, frame):
        name = next(iter(self.stores))
        store, engine = self.stores[name], self.engines[name]
        if float(store.get("last_tick", "0")) < frame.ts:
            account, row, fills = engine.calculate(frame)
            with store.transaction():
                for fill in fills:
                    store.db.execute("INSERT INTO fills VALUES (?,?,?)", (fill["id"], frame.ts, encoded(fill)))
                store.db.execute("INSERT INTO ticks(ts,snapshot,account) VALUES (?,?,?)", (frame.ts, encoded(row), encoded(account)))
                store.set("account", encoded(account))
                store.set("last_tick", frame.ts)
        else:
            row, account = store.snapshot(), store.account()
        if float(store.get("last_tick")) != frame.ts:
            raise GridError("CL scalper ledger and frame differ")
        old = self.latest()
        if old and old["ts"] >= frame.ts:
            if old["ts"] != frame.ts:
                raise GridError("Cannot republish an older CL scalper frame")
            return old
        quotes = account["last_quotes"]
        pause = account["pair_pause"]
        summary = {"mode": self.experiment.kind, "ts": frame.ts, "time_utc": utc(frame.ts), "data_kind": frame.data_kind,
            "started_utc": old["started_utc"] if old else utc(frame.ts), "sample_count": old["sample_count"] + 1 if old else 1,
            "center": None, "center_window_hours": None, "poll_seconds": self.experiment.settings.poll_seconds,
            "pnl_basis": "before_funding", "scenarios": [{"name": name, **row}], "market": {
                "cl_source_ts": quotes.get("CL", {}).get("ts"), "bz_source_ts": quotes.get("BZ", {}).get("ts"),
                "source_status": "paused" if pause["active"] else "ready",
                "segment": account["segment"], "cl_mark": quotes.get("CL", {}).get("mark"), "bz_mark": quotes.get("BZ", {}).get("mark"),
                "spread_bz_minus_cl": str(dec(quotes["BZ"]["mark"]) - dec(quotes["CL"]["mark"])) if quotes else None}}
        with self.db:
            self.db.execute("INSERT INTO summaries VALUES (?,?)", (frame.ts, encoded(summary)))
        self.set_runtime("paused" if pause["active"] else "running", pause.get("reason"))
        return summary

    def report(self):
        row = self.db.execute("SELECT payload FROM runtime WHERE id=1").fetchone()
        public = self.experiment.output / "public"
        public.mkdir(exist_ok=True)
        write_json(public / "summary.json", {"runtime": json.loads(row[0]) if row else {}, "summary": self.latest()})


class ScalperFeed:
    def __init__(self, experiment, client=None):
        self.experiment = experiment
        self.client = client or CommodityClient(experiment.base.session_file)
        self.markets = {}

    def next(self):
        from .cli import paired
        def observe(symbol):
            try:
                return self.client.market_observation(symbol)
            except GridError:
                return {**self.markets.get(symbol, {}), "state": "unknown"}
        self.markets = dict(zip(("CL", "BZ"), paired(observe)))
        reason, _ = market_gate(self.markets, time.time())
        quotes = {}
        if not reason:
            try:
                observations = paired(lambda symbol: self.client.quote(symbol, dec(self.experiment.settings.quantity_barrels)))
                validate_pair(*observations, time.time(), self.experiment.settings)
                quotes = {q.symbol: {k: v for k, v in asdict(q).items() if k != "symbol"} for q in observations}
                quotes = json.loads(encoded(quotes))
            except GridError:
                reason = "CL / BZ 有效报价暂不可用，暂停模拟订单"
        now = time.time()
        gate, _ = market_gate(self.markets, now)
        if gate:
            reason, quotes = gate, {}
        frame = ScalperFrame(now, self.markets, quotes, reason)
        frame.validate(self.experiment)
        return frame


def run_scalper(args, experiment):
    from .cli import emit
    from .reset import process_reset, read_state
    feed = ScalperFeed(experiment)
    with ScalperCohort(experiment) as cohort:
        store = next(iter(cohort.stores.values()))
        feed.markets = store.account()["pair_pause"].get("markets", {})
        stop = experiment.output / "STOP"
        if stop.exists():
            stop.unlink()
        count = 0
        while not stop.exists():
            started = time.monotonic()
            process_reset(cohort)
            result = cohort.ingest(feed.next())
            emit({"mode": result["mode"], "sample_count": result["sample_count"], "source_status": result["market"]["source_status"]})
            count += 1
            if args.once or args.iterations and count >= args.iterations:
                return 0
            while time.monotonic() - started < experiment.settings.poll_seconds and not stop.exists():
                if read_state(experiment)["status"] in {"pending", "archiving", "clearing"}:
                    break
                time.sleep(1)
        stop.unlink()
    return 0


def read_snapshot(experiment):
    from .reset import control_lock, read_state
    with control_lock(experiment):
        reset = read_state(experiment)
        result = {"kind": experiment.kind, "server_ts": time.time(), "summary": None, "runtime": {"status": "starting"},
                  "parameters": experiment.settings.parameters(), "pair_pause": {}, "positions": [], "trades": [], "orders": [],
                  "details_available": False, "reset": reset}
        path = experiment.output / "comparison.sqlite3"
        if not path.exists() or reset and reset["status"] in {"archiving", "clearing"}:
            return result
        manifest = json.loads((experiment.output / "experiment.json").read_text(encoding="utf-8"))
        if manifest != experiment.identity():
            raise GridError("CL scalper dashboard configuration differs from stored experiment")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT payload FROM runtime WHERE id=1").fetchone()
            runtime = json.loads(row[0]) if row else {"status": "starting"}
            result["runtime"] = {key: runtime.get(key) for key in ("status", "updated_utc")}
            if runtime.get("status") == "paused":
                result["runtime"]["reason"] = "双腿模拟暂停，详见市场联动状态"
            row = db.execute("SELECT payload FROM summaries ORDER BY ts DESC LIMIT 1").fetchone()
            if not row:
                return result
            result["summary"] = summary = json.loads(row[0])
        config = experiment.settings
        with closing(sqlite3.connect(Path(config.state_file).as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            row = db.execute("SELECT account FROM ticks WHERE ts=?", (summary["ts"],)).fetchone()
        if row is None:
            return result
        account = json.loads(row[0])
        quotes = account["last_quotes"]
        for slot in account["slots"]:
            cl = floating_pnl("CL", dec(slot["qty"]), dec(slot["entry_cl"]), quotes, config)
            bz = floating_pnl("BZ", -dec(slot["qty"]), dec(slot["entry_bz"]), quotes, config)
            result["positions"].append({**slot, "scenario": next(iter(experiment.scenarios)), "cl_unrealized_pnl_usdc": str(cl),
                "bz_unrealized_pnl_usdc": str(bz), "unrealized_pnl_usdc": str(cl + bz), "valued_at": min(q["ts"] for q in quotes.values())})
        result.update(pair_pause=account["pair_pause"], orders=account["orders"], trades=list(reversed(account["closed_batches"])), details_available=True)
        return result


def read_history(experiment, window, through=None):
    from .reset import control_lock, read_state
    windows = {"1h": 3600, "24h": 86400, "7d": 604800}
    if window not in windows or through is not None and not fresh(through, through, 1):
        raise GridError("Invalid CL scalper history range")
    # Only pin the SQLite read transaction and reset generation under the lock.
    # A chart scan then uses its WAL snapshot without blocking current cards.
    from contextlib import ExitStack
    with ExitStack() as stack:
        with control_lock(experiment):
            reset = read_state(experiment)
            path = experiment.output / "comparison.sqlite3"
            result = {"server_ts": time.time(), "summary_ts": None, "reset_generation": reset.get("generation") if reset else None,
                      "history": {"range": window, "names": list(experiment.scenarios), "source_count": 0, "points": []}}
            if not path.exists() or reset and reset["status"] in {"archiving", "clearing"}:
                return result
            if json.loads((experiment.output / "experiment.json").read_text(encoding="utf-8")) != experiment.identity():
                raise GridError("CL scalper history configuration differs from stored experiment")
            db = stack.enter_context(closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)))
            deadline = time.monotonic() + 3
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            db.execute("BEGIN")
            end = db.execute("SELECT MAX(ts) FROM summaries WHERE ts<=?", (through or time.time(),)).fetchone()[0]
        if end is None:
            return result
        result["summary_ts"] = end
        start = end - windows[window]
        count = db.execute("SELECT COUNT(*) FROM summaries WHERE ts BETWEEN ? AND ?", (start, end)).fetchone()[0]
        result["history"]["source_count"] = count
        # Persisted segments prevent bridging even a downsampled-away closure.
        stride = max(1, math.ceil(count / 900))
        rows = db.execute("SELECT payload FROM (SELECT payload,ROW_NUMBER() OVER (ORDER BY ts) n FROM summaries WHERE ts BETWEEN ? AND ?) WHERE (n-1)%?=0 OR n=?", (start, end, stride, count))
        for raw, in rows:
            if time.monotonic() > deadline:
                raise GridError("CL scalper history deadline exceeded")
            sample = json.loads(raw)
            paused = sample["market"]["source_status"] != "ready"
            result["history"]["points"].append({"ts": sample["ts"], "spread": None if paused else sample["market"]["spread_bz_minus_cl"],
                "center": None, "segment": sample["market"]["segment"], "pnl": [None if paused else row["total_pnl_usdc"] for row in sample["scenarios"]]})
        return result
