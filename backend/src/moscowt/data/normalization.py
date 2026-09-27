"""Streaming source readers. Only aggregated route-hour data is kept in memory."""

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..constants.data_normalization import (
    KEYS as KEYS,
)
from ..domain import TZ, DomainError
from .schemas import ImportSpec


@dataclass
class NormalizedImport:
    frame: pd.DataFrame
    input_rows: int = 0
    selected_rows: int = 0
    event_files: list[str] = field(default_factory=list)


class SourceNormalizer:
    def __init__(self, chunk_size=100_000):
        self.chunk_size = chunk_size

    @staticmethod
    def _routes(values):
        routes = values.astype(str).str.strip()
        if not routes.str.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}").all():
            raise DomainError("INVALID_ROUTE_ID", "Некорректный идентификатор маршрута")
        return routes

    @staticmethod
    def _selected(frame, spec):
        keep = (frame.timestamp >= spec.time_range.start) & (frame.timestamp < spec.time_range.end)
        if spec.route_ids:
            keep &= frame.route.isin(spec.route_ids)
        return frame.loc[keep].copy()

    @staticmethod
    def _labels(frame):
        needed = {"route", "date", "hour", "boardings"}
        if not needed.issubset(frame):
            raise DomainError("INVALID_LABEL_COLUMNS", "Нужны route;date;hour;boardings")
        for name in ("hour", "boardings"):
            if not frame[name].str.fullmatch(r"\d+").all():
                raise DomainError("INVALID_LABEL_VALUE", f"{name}: ожидается целое неотрицательное число")
        hours = pd.to_numeric(frame.hour)
        if not hours.between(0, 23).all() or not frame.date.str.fullmatch(r"\d{4}-\d{2}-\d{2}").all():
            raise DomainError("INVALID_LABEL_TIME", "Нужны дата YYYY-MM-DD и час 0–23")
        dates = pd.to_datetime(frame.date, format="%Y-%m-%d", errors="raise").dt.tz_localize(TZ)
        # Reject parser normalization of impossible dates and integer overflow.
        if not dates.dt.strftime("%Y-%m-%d").eq(frame.date).all():
            raise DomainError("INVALID_LABEL_TIME", "Некорректная дата")
        values = pd.to_numeric(frame.boardings).astype(float)
        if not np.isfinite(values).all():
            raise DomainError("INVALID_LABEL_VALUE", "Значения должны быть конечными")
        return pd.DataFrame(
            {
                "route": SourceNormalizer._routes(frame.route),
                "timestamp": dates + pd.to_timedelta(hours, unit="h"),
                "value": values,
            }
        )

    @staticmethod
    def _events(frame):
        needed = {"ngpt_route", "tran_date_time", "validation_result", "bus_exit_no", "garage_number"}
        if not needed.issubset(frame):
            raise DomainError(
                "INVALID_EVENT_COLUMNS", "Не хватает времени, маршрута, результата или номеров ТС"
            )
        route = frame.ngpt_route.str.strip().str.replace(r"\s+трамвай$", "", regex=True)
        stamps = pd.to_datetime(frame.tran_date_time, format="mixed", dayfirst=True, errors="raise")
        if stamps.dt.tz is None:
            stamps = stamps.dt.tz_localize(TZ)
        else:
            stamps = stamps.dt.tz_convert(TZ)
        if not frame.validation_result.str.fullmatch(r"\d+").all():
            raise DomainError("INVALID_EVENT_RESULT", "Некорректный результат валидации")
        return pd.DataFrame(
            {
                "route": SourceNormalizer._routes(route),
                "event_time": stamps,
                "timestamp": stamps.dt.floor("h"),
                "value": pd.to_numeric(frame.validation_result).eq(1).astype(int),
                "bus_exit_no": frame.bus_exit_no.str.strip(),
                "garage_number": frame.garage_number.str.strip(),
            }
        )

    def read(self, sources: list[Path], spec: ImportSpec, destination: Path):
        batches = []
        result = NormalizedImport(pd.DataFrame())
        for source in sources:
            try:
                reader = pd.read_csv(
                    source,
                    sep=";",
                    dtype=str,
                    keep_default_na=False,
                    encoding="utf-8-sig",
                    chunksize=self.chunk_size,
                )
                for chunk in reader:
                    result.input_rows += len(chunk)
                    frame = self._labels(chunk) if spec.kind == "labels" else self._events(chunk)
                    frame = self._selected(frame, spec)
                    result.selected_rows += len(frame)
                    if spec.kind == "events" and not frame.empty:
                        filename = f"events-{len(result.event_files):05d}.parquet"
                        frame.to_parquet(destination / filename, index=False)
                        result.event_files.append(filename)
                        frame = frame.groupby(KEYS, as_index=False).value.sum()
                    batches.append(frame[KEYS + ["value"]])
                    if spec.kind == "events" and len(batches) >= 8:
                        batches = [pd.concat(batches).groupby(KEYS, as_index=False).value.sum()]
            except (ValueError, OverflowError, pd.errors.ParserError) as exc:
                raise DomainError("INVALID_SOURCE", f"Не удалось разобрать CSV: {exc}") from exc
        if not batches:
            raise DomainError("EMPTY_UPLOAD", "В файлах нет строк")
        frame = pd.concat(batches, ignore_index=True)
        if spec.kind == "events":
            frame = frame.groupby(KEYS, as_index=False).value.sum()
        elif frame.duplicated(KEYS).any():
            raise DomainError("DUPLICATE_LABEL_KEY", "Повторяется ключ маршрут × час")
        routes = spec.route_ids or sorted(frame.route.unique().tolist())
        if not routes:
            raise DomainError("EMPTY_SELECTION", "В выбранном периоде нет данных")
        size = len(routes) * int((spec.time_range.end - spec.time_range.start).total_seconds() // 3600)
        if size > 5_000_000:
            raise DomainError("IMPORT_TOO_LARGE", "Разделите импорт: не более 5 млн маршрутно-часовых ключей")
        index = pd.MultiIndex.from_product(
            [routes, pd.date_range(spec.time_range.start, spec.time_range.end, freq="h", inclusive="left")],
            names=KEYS,
        )
        frame = frame.set_index(KEYS).reindex(index).reset_index()
        provided = frame.value.notna()
        frame["value_origin"] = np.where(
            provided,
            "label" if spec.kind == "labels" else "events",
            "filled_zero" if spec.complete else "missing",
        )
        if spec.complete:
            frame["value"] = frame.value.fillna(0)
        frame["coverage"] = np.where(frame.value.notna(), "provided_extract", "missing")
        frame["timestamp"] = pd.to_datetime(frame.timestamp).astype("datetime64[ns, Europe/Moscow]")
        result.frame = frame.sort_values(KEYS).reset_index(drop=True)
        return result
