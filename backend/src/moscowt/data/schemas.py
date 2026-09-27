"""Public data ingestion contract, independent of competition dates and routes."""

from typing import Literal

from pydantic import Field, model_validator

from ..domain import StrictModel, TimeRange


class ImportSpec(StrictModel):
    kind: Literal["labels", "events"] = "labels"
    mode: Literal["append", "upsert", "replace"] = "append"
    time_range: TimeRange
    route_ids: list[str] = Field(default_factory=list, max_length=1000)
    complete: bool = False
    dataset_name: str | None = Field(default=None, min_length=1, max_length=160)
    base_dataset_id: str | None = None
    new_dataset: bool = False

    @model_validator(mode="after")
    def valid_selection(self):
        if self.new_dataset and self.base_dataset_id:
            raise ValueError("Новый набор не может иметь исходную редакцию")
        if len(set(self.route_ids)) != len(self.route_ids):
            raise ValueError("Маршруты не должны повторяться")
        if self.mode == "replace" and not self.route_ids:
            raise ValueError("Для замены периода укажите маршруты явно")
        if (self.time_range.end - self.time_range.start).days > 3660:
            raise ValueError("Один импорт ограничен десятью годами")
        return self
