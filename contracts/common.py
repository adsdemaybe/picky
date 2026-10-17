"""Shared primitives for every contract: base model, id aliases, money, geometry, time.

Conventions (CLAUDE.md §7):
- Money is integer cents (``Cents``), never float.
- Units are meters and radians in the store world frame: x right, y up, origin at the bottom-left corner of the
  store, theta measured counter-clockwise from +x.
- Timestamps are timezone-aware UTC datetimes (``Timestamp``); naive values and bare dates are coerced to UTC.
"""

import math
from datetime import UTC, date, datetime, time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


class Model(BaseModel):
    """Base for every contract model.

    - Unknown fields are rejected (``extra="forbid"``) so drift between arms on the same checkout fails loudly.
    - Derived values (cart totals, ``Route.extra_distance_m``, ...) are ``@computed_field``s: they appear in JSON and
      in the generated TS types, and are silently dropped again when a dumped dict/JSON is validated back.
    - The serialization JSON schema marks every field with a default as required (it is always present in output),
      so generated TS types have non-optional ``type``/``seq``/``ts``/... fields.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        use_enum_values=False,
        json_schema_serialization_defaults_required=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_computed_fields(cls, data: Any) -> Any:
        computed = cls.model_computed_fields
        if computed and isinstance(data, dict) and any(k in computed for k in data):
            return {k: v for k, v in data.items() if k not in computed}
        return data


class WireModel(Model):
    """Base for messages that cross machines (gateway <-> Isaac worker, contracts/sim.py).

    The two ends run from different checkouts, so unknown fields are IGNORED instead of rejected: an additive (patch)
    contract change on one side never breaks the link. Major/minor mismatches are caught by the SimHello version check.
    """

    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------------------------------------------------
# Id aliases (plain strings; aliases document intent and keep signatures readable)
# --------------------------------------------------------------------------------------------------------------------
Sku = str
StoreId = str
UserId = str
TripId = str
SlotId = str
FixtureId = str
AisleId = str
LotId = str
DealId = str
RecId = str
ItemId = str
ListId = str
LineId = str
ApprovalId = str
InstanceId = str
FrameId = str
CmdId = str
RouteId = str
StopId = str
ScanId = str
MessageId = str
MandateId = str
ReceiptId = str
OrderId = str
FeedbackId = str
EventId = str

EMBED_DIM = 384
"""Embedding size for product/profile vectors (BAAI/bge-small-en-v1.5). Mirrored by vector(384) in db/schema.sql."""

ONLINE_STORE_ID: StoreId = "online"
"""Store id of the mock online store (no planogram)."""

DEFAULT_STORE_ID: StoreId = "store_ave"

ROBOT_RADIUS_M = 0.6
"""THE robot footprint radius (meters), agreed by routing, robot, sim and data.

~ circumradius of the Ridgeback-Franka base (0.96 x 0.79 m -> 0.62 m) with the arm stowed. Routing inflates obstacles
by exactly this (``GridSpec.robot_radius_m`` default, no extra margin); the robot fake / sim use it for collisions.
Data rules (checked by ``python -m utils.data``): every approach pose, the entrance and the checkout pose are at least
this far from every fixture rect, obstacle and the store outline, so aisles must be >= 2 x ROBOT_RADIUS_M + 0.1 m
(1.3 m) wide; design them 1.5 m.
"""

# --------------------------------------------------------------------------------------------------------------------
# Scalars
# --------------------------------------------------------------------------------------------------------------------
Cents = Annotated[int, Field(ge=0, description="Money in integer cents (USD).")]
SignedCents = Annotated[int, Field(description="Signed money delta in integer cents.")]
Score01 = Annotated[float, Field(ge=0.0, le=1.0)]
Rating = Annotated[int, Field(ge=1, le=5)]
Qty = Annotated[int, Field(ge=0)]
PositiveQty = Annotated[int, Field(ge=1)]
Meters = float
Radians = float
JsonScalar = str | int | float | bool | None

Channel = Literal["web", "voice", "photon"]
"""User-facing channel a message or approval travels on."""

CameraName = Literal["head", "wrist", "basket", "overview"]
Currency = Literal["USD"]


def _coerce_timestamp(value: Any) -> Any:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    return value


Timestamp = Annotated[datetime, BeforeValidator(_coerce_timestamp)]
"""UTC datetime. Accepts aware/naive datetimes, ISO strings, dates (midnight UTC) and unix seconds."""


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------------------------------------
# Geometry (world frame, meters / radians)
# --------------------------------------------------------------------------------------------------------------------
def normalize_angle(theta: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    wrapped = math.atan2(math.sin(theta), math.cos(theta))
    return math.pi if wrapped == -math.pi else wrapped


def angle_diff(a: float, b: float) -> float:
    """Smallest signed difference ``a - b`` wrapped to (-pi, pi]."""
    return normalize_angle(a - b)


class Point2(Model):
    x: Meters
    y: Meters

    def distance_to(self, other: "Point2 | Pose2D") -> float:
        return math.hypot(other.x - self.x, other.y - self.y)


class Point3(Model):
    """3D point in the store/sim world frame: (x, y) as in Pose2D, z = height above the floor (meters, Z-up)."""

    x: Meters
    y: Meters
    z: Meters = 0.0


class Pose2D(Model):
    """Planar pose in the store world frame. theta is CCW from +x, radians."""

    x: Meters
    y: Meters
    theta: Radians = 0.0

    def distance_to(self, other: "Pose2D | Point2") -> float:
        return math.hypot(other.x - self.x, other.y - self.y)

    def as_point(self) -> Point2:
        return Point2(x=self.x, y=self.y)


class Rect2D(Model):
    """Axis-aligned rectangle in the world frame: (x, y) is the min (bottom-left) corner, w along +x, h along +y."""

    x: Meters
    y: Meters
    w: Annotated[float, Field(gt=0)]
    h: Annotated[float, Field(gt=0)]

    @property
    def x_max(self) -> float:
        return self.x + self.w

    @property
    def y_max(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> Point2:
        return Point2(x=self.x + self.w / 2, y=self.y + self.h / 2)

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x_max and self.y <= y <= self.y_max

    def distance_to(self, x: float, y: float) -> float:
        """Euclidean distance from (x, y) to the rectangle (0 when inside or on the edge)."""
        dx = max(self.x - x, 0.0, x - self.x_max)
        dy = max(self.y - y, 0.0, y - self.y_max)
        return math.hypot(dx, dy)

    def inflated(self, margin: float) -> "Rect2D":
        """Copy grown by ``margin`` on every side (shrunk if negative)."""
        return Rect2D(x=self.x - margin, y=self.y - margin, w=self.w + 2 * margin, h=self.h + 2 * margin)

    def intersects(self, other: "Rect2D") -> bool:
        return not (other.x >= self.x_max or other.x_max <= self.x or other.y >= self.y_max or other.y_max <= self.y)


class BBox(Model):
    """Pixel bounding box in an image: (x, y) top-left corner, w/h in pixels."""

    x: float
    y: float
    w: Annotated[float, Field(ge=0)]
    h: Annotated[float, Field(ge=0)]


def money(cents: int) -> str:
    """Format integer cents for display: 1234 -> '$12.34' (negative -> '-$12.34')."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def ensure_unique(values: list[str], what: str) -> None:
    """Raise ValueError naming duplicates (used by model validators for unique ids)."""
    seen: set[str] = set()
    dupes: list[str] = []
    for v in values:
        if v in seen and v not in dupes:
            dupes.append(v)
        seen.add(v)
    if dupes:
        raise ValueError(f"duplicate {what}: {', '.join(dupes)}")
