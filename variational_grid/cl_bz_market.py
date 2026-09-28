"""Validated CL/BZ commodity metadata; no trading endpoints."""
import math
from .client import Client
from .models import GridError, timestamp

LEAD_SECONDS = 300
METADATA_MAX_AGE = 120

def fresh(value, now, age):
    return type(value) in (float, int) and math.isfinite(value) and value > 0 and -2 <= now - value <= age

class CommodityClient(Client):
    def market_observation(self, symbol):
        data, received, source = self.request("GET", "/metadata/supported_assets", query={"cex_asset": symbol}, with_observation=True)
        try:
            rows = [row for row in data[symbol] if row.get("asset") == symbol and row.get("has_perp") is True]
            if len(rows) != 1:
                raise GridError("CL/BZ metadata unavailable")
            row = rows[0]
            if row["instrument_type"] != "perpetual_rwa_future" or row["asset_class"] != "commodity":
                raise GridError("CL/BZ commodity instrument changed")
            if row["market_status"] not in {"open", "closed"} or type(row["is_close_only_mode"]) is not bool:
                raise GridError("Invalid CL/BZ market status")
            if not fresh(source, received, METADATA_MAX_AGE):
                raise GridError("CL/BZ market metadata is stale or lacks a source timestamp")
            sessions = row.get("trading_sessions")
            closes_at, in_session = None, True
            if sessions is not None:
                if not isinstance(sessions, list) or not sessions or len(sessions) > 1000:
                    raise GridError("Invalid CL/BZ trading sessions")
                in_session = False
                for session in sessions:
                    opens, closes = timestamp(session["open"]), timestamp(session["close"])
                    if opens >= closes:
                        raise GridError("Invalid CL/BZ trading session interval")
                    if opens <= received < closes:
                        in_session, closes_at = True, closes
            state = "closed" if row["market_status"] == "closed" or not in_session else "close_only" if row["is_close_only_mode"] else "open"
            return {"state": state, "source_ts": source, "closes_at": closes_at, "schedule_known": sessions is not None}
        except (KeyError, TypeError, AttributeError):
            raise GridError("CL/BZ market metadata schema changed") from None

def market_gate(markets, now):
    closed, closing, unavailable = [], [], []
    for symbol in ("CL", "BZ"):
        row = markets.get(symbol, {})
        close = row.get("closes_at")
        if close is not None and (type(close) not in (float, int) or not math.isfinite(close) or close <= 0):
            raise GridError("Invalid CL/BZ closing time")
        if row.get("state") == "closed" or close is not None and now >= close:
            closed.append(symbol)
        elif close is not None and now >= close - LEAD_SECONDS:
            closing.append(symbol)
        if row.get("state") != "open" or not fresh(row.get("source_ts"), now, METADATA_MAX_AGE):
            unavailable.append(symbol)
    if closed:
        return " / ".join(closed) + " 休市，暂停双腿开仓与止盈触发", max([now] + [r["closes_at"] for r in markets.values() if r.get("closes_at") and now >= r["closes_at"] - LEAD_SECONDS])
    if closing:
        return " / ".join(closing) + " 将在5分钟内休市，暂停双腿开仓与止盈触发", max([now] + [markets[s]["closes_at"] for s in closing])
    if unavailable:
        return " / ".join(unavailable) + " 行情未知、过期或只减仓，暂停双腿开仓与止盈触发", now
    return None, None
