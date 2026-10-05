from pydantic import BaseModel


Coordinate = tuple[float, float]


class ActivitySummary(BaseModel):
    id: str
    name: str
    date: str
    type: str
    processed: bool
    unmapped_points: int


class ActivityPage(BaseModel):
    items: list[ActivitySummary]
    page: int
    page_size: int
    total: int


class ActivityDetail(ActivitySummary):
    tracks: list[list[Coordinate]]
    timestamps: list[list[str | None]]
    bounds: tuple[Coordinate, Coordinate] | None
