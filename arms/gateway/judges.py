"""Judges' demo state the gateway keeps from its own event stream (contracts 0.2.4): photo verification cards
(``item.verification``) and the Visa payment agent timeline (``payment.agent_step``), for the REST routes
``GET /api/verifications``, ``POST /api/verifications/{id}`` and ``GET /api/trips/{trip_id}/payment``, and the
verification photos stored with the purchase rows (``photos(trip_id, sku)``)."""

from collections import OrderedDict
from typing import Any

from contracts.trust import ItemVerification, PaymentAgentStep

MAX_VERIFICATIONS = 2000
MAX_TRIPS = 200


class JudgesIndex:
    def __init__(self) -> None:
        self.verifications: OrderedDict[str, ItemVerification] = OrderedDict()
        self.payments: OrderedDict[str, list[PaymentAgentStep]] = OrderedDict()

    def observe(self, event: Any) -> None:
        t = getattr(event, "type", None)
        if t == "item.verification":
            v: ItemVerification = event.verification
            self.verifications[v.verification_id] = v
            self.verifications.move_to_end(v.verification_id)
            while len(self.verifications) > MAX_VERIFICATIONS:
                self.verifications.popitem(last=False)
        elif t == "payment.agent_step":
            step: PaymentAgentStep = event.step
            steps = self.payments.setdefault(step.trip_id, [])
            steps[:] = [s for s in steps if s.step_id != step.step_id] + [step]
            while len(self.payments) > MAX_TRIPS:
                self.payments.popitem(last=False)

    def query(
        self, *, trip_id: str | None = None, user_id: str | None = None, status: str | None = None
    ) -> list[ItemVerification]:
        out = [
            v
            for v in self.verifications.values()
            if (trip_id is None or v.trip_id == trip_id)
            and (user_id is None or v.user_id == user_id)
            and (status is None or v.status == status)
        ]
        return sorted(out, key=lambda v: v.created_at, reverse=True)

    def photos(self, trip_id: str | None, sku: str) -> list[str]:
        """Accepted / auto-accepted photos of ``sku`` in that trip (newest last) for the purchase rows."""
        return [
            v.photo_url
            for v in self.verifications.values()
            if v.trip_id == trip_id and v.sku == sku and v.photo_url and v.status in ("accepted", "auto_accepted")
        ]

    def timeline(self, trip_id: str) -> list[PaymentAgentStep]:
        return sorted(self.payments.get(trip_id, []), key=lambda s: s.n)


__all__ = ["JudgesIndex"]
