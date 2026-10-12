from pydantic import BaseModel


Coordinate = tuple[float, float]


class ActivitySummary(BaseModel):
    id: str
    name: str
    date: str
    type: str
    processed: bool
    unmapped_points: int
    distance_m: float | None = None
    duration_s: float | None = None
    pace_s_per_km: float | None = None


class ActivityPage(BaseModel):
    items: list[ActivitySummary]
    page: int
    page_size: int
    total: int


class ActivityDetail(ActivitySummary):
    tracks: list[list[Coordinate]]
    timestamps: list[list[str | None]]
    bounds: tuple[Coordinate, Coordinate] | None
