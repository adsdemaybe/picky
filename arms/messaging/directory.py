"""Phone/email handle <-> user_id mapping (shared by fake and real).

Primary source: ``services.recommender`` (owner of profiles), called at call time only. Fallback when the recommender
isn't wired or fails: the demo fixtures in ``data/fixtures/users.yaml`` (via ``utils.data``). Both sides are compared
after ``contracts.messaging.normalize_handle``.
"""

from contracts.common import UserId
from contracts.messaging import normalize_handle
from contracts.services import Services
from utils.log import get_logger

log = get_logger(__name__)


def _fixture_profiles() -> list:
    try:
        from utils.data import DataError, load_demo_fixtures
    except ImportError:  # pragma: no cover - utils.data always ships
        return []
    try:
        return [u.profile for u in load_demo_fixtures().users]
    except DataError as exc:
        log.debug("fixtures unavailable: %s", exc)
        return []


class HandleDirectory:
    """Resolves handles both ways; caches what it learns (e.g. handles seen on inbound texts)."""

    def __init__(self, services: Services) -> None:
        self._services = services
        self._user_by_handle: dict[str, UserId] = {}
        self._handle_by_user: dict[UserId, str] = {}

    def remember(self, handle: str, user_id: UserId) -> None:
        h = normalize_handle(handle)
        self._user_by_handle[h] = user_id
        self._handle_by_user[user_id] = h

    async def user_for_handle(self, handle: str | None) -> UserId | None:
        if not handle:
            return None
        h = normalize_handle(handle)
        if h in self._user_by_handle:
            return self._user_by_handle[h]
        rec = self._services.recommender
        if rec is not None:
            try:
                profile = await rec.find_user_by_handle(h)
            except Exception as exc:  # noqa: BLE001 - degrade to fixtures
                log.warning("recommender.find_user_by_handle failed: %s", exc)
                profile = None
            if profile is not None:
                self.remember(h, profile.user_id)
                return profile.user_id
        for p in _fixture_profiles():
            if any(x and normalize_handle(x) == h for x in (p.phone, p.email)):
                self.remember(h, p.user_id)
                return p.user_id
        return None

    async def handle_for_user(self, user_id: UserId | None) -> str | None:
        if not user_id:
            return None
        if user_id in self._handle_by_user:
            return self._handle_by_user[user_id]
        rec = self._services.recommender
        profile = None
        if rec is not None:
            try:
                profile = await rec.get_profile(user_id)
            except Exception as exc:  # noqa: BLE001 - KeyError for unknown users, or recommender down
                log.debug("recommender.get_profile(%s) failed: %s", user_id, exc)
        if profile is None:
            profile = next((p for p in _fixture_profiles() if p.user_id == user_id), None)
        handle = (profile.phone or profile.email) if profile is not None else None
        if handle:
            self.remember(handle, user_id)
            return normalize_handle(handle)
        return None
