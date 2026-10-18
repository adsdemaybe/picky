"""Shared stubs for messaging tests (base deps only)."""

import pytest

from contracts.messaging import normalize_handle
from contracts.profile import UserProfile
from contracts.services import Services
from contracts.trust import ApprovalResponse

MAYA_PHONE = "+15555550100"  # fictional (555-01xx is reserved for fiction)


class StubRecommender:
    """Just the two methods messaging calls."""

    def __init__(self) -> None:
        self.profiles = {
            "maya": UserProfile(user_id="maya", display_name="Maya", phone=MAYA_PHONE),
        }

    async def get_profile(self, user_id: str) -> UserProfile:
        return self.profiles[user_id]

    async def find_user_by_handle(self, handle: str) -> UserProfile | None:
        h = normalize_handle(handle)
        return next((p for p in self.profiles.values() if p.phone and normalize_handle(p.phone) == h), None)


class StubEvents:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event):  # type: ignore[no-untyped-def]
        self.events.append(event)
        return event


class StubApprovals:
    def __init__(self) -> None:
        self.responses: list[ApprovalResponse] = []

    async def request(self, request):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def resolve(self, response: ApprovalResponse) -> bool:
        self.responses.append(response)
        return True

    def pending(self, trip_id=None):  # type: ignore[no-untyped-def]
        return []


@pytest.fixture
def services() -> Services:
    return Services(recommender=StubRecommender(), events=StubEvents(), approvals=StubApprovals())  # type: ignore[arg-type]
