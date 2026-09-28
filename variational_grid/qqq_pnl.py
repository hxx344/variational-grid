"""Read-only batch attribution for the fixed-price QQQ paper entry models."""
from decimal import DecimalException

from .models import D, GridError, dec


def batch_pnl(account, mark, fee_bps):
    """Reassign average-cost PnL to actual slots without changing the ledger.

    All QQQ entries fill at slot.entry_price; repricing creates a new slot.
    Fees are fixed by the experiment identity. Remaining inventory therefore
    suffices to recover cumulative closed-batch PnL, including partial exits,
    even from old accounts that never stored a batch PnL accumulator.
    """
    unavailable = {"status": "unavailable", "reason": "batch_cost_unavailable"}
    try:
        leg, slots = account["qqq"], account["slots"]
        qty, avg = dec(leg["qty"]), dec(leg["average_entry"])
        realized, fees = dec(leg["realized_gross"]), dec(leg["fees_usdc"])
        mark, rate = dec(mark), dec(fee_bps) / 10000
        if not isinstance(slots, list) or qty < 0 or avg < 0 or fees < 0 or mark <= 0 or not 0 <= rate <= D('.01'):
            return unavailable
        if qty > 0 and avg <= 0 or qty == 0 and avg != 0:
            return unavailable
        cost, held, identities = D(0), D(0), set()
        for slot in slots:
            identity = slot["slot"]
            if type(identity) is not int or identity in identities:
                return unavailable
            identities.add(identity)
            quantity = dec(slot["qty"])
            if quantity < 0:
                return unavailable
            held += quantity
            if quantity:
                entry = dec(slot["entry_price"])
                if entry <= 0:
                    return unavailable
                cost += quantity * entry
        if held != qty:
            return {**unavailable, "reason": "batch_quantity_mismatch"}
        open_fees = cost * rate
        closed_fees = fees - open_fees
        # Only absorb sub-cent decimal arithmetic noise, never economic losses.
        if closed_fees < 0:
            if closed_fees < -D('1e-18'):
                return {**unavailable, "reason": "batch_fee_mismatch"}
            closed_fees = D(0)
        gross = realized + cost - qty * avg
        net = gross - closed_fees
        floating = qty * mark - cost - open_fees
        return {"status": "ready", "basis": "qqq_slot_cost_v1",
                "gross_pnl_usdc": str(gross), "closed_fees_usdc": str(closed_fees),
                "net_pnl_usdc": str(net), "remaining_pnl_usdc": str(floating)}
    except (KeyError, TypeError, GridError, DecimalException):
        return unavailable
