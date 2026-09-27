import pandas as pd

from moscowt.external import ExternalRepository, external_features
from moscowt.schedules import ScheduleService
from moscowt.spatial import SpatialService


def test_external_cutoff_and_known_event(model_data, tmp_path):
    data, ident, _ = model_data
    source = tmp_path / "weather"
    source.mkdir()
    dates = pd.date_range("2024-01-01", "2025-12-31")
    weather = pd.DataFrame(
        {
            "date": dates,
            "temperature_2m_mean": 5.0,
            "precipitation_sum": 1.0,
            "snowfall_sum": 0.0,
            "wind_speed_10m_mean": 2.0,
        }
    )
    weather.to_parquet(source / "weather_history_2014_2025_09.parquet", index=False)
    (source / "sources.json").write_text("{}")
    repository = ExternalRepository(data.root)
    manifest = repository.register(source)
    target = pd.DataFrame(
        {
            "route": ["7", "7"],
            "timestamp": pd.to_datetime(
                ["2025-08-20T12:00:00+03:00", "2025-09-20T12:00:00+03:00"]
            ).tz_convert("Europe/Moscow"),
        }
    )
    first = external_features(
        data.root, manifest["id"], target, pd.Timestamp("2025-08-01T00:00:00+03:00"), ["weather", "events"]
    )
    assert first.known_service_change.tolist() == [0, 0]
    weather.loc[weather.date.dt.year == 2025, "temperature_2m_mean"] = 10000
    weather.to_parquet(source / "weather_history_2014_2025_09.parquet", index=False)
    changed = repository.register(source)
    second = external_features(
        data.root, changed["id"], target, pd.Timestamp("2025-08-01T00:00:00+03:00"), ["weather", "events"]
    )
    pd.testing.assert_frame_equal(first, second)
    later = external_features(
        data.root, changed["id"], target, pd.Timestamp("2025-09-01T00:00:00+03:00"), ["events"]
    )
    assert later.known_service_change.tolist() == [1, 0]


def test_spatial_stops_conserve_and_segments_are_not_additive(store):
    service = SpatialService(store)
    snapshot = store.current()
    compiled = service.compile(snapshot["networkId"], "2025-11-01")
    route = next(iter(compiled))
    for phase in ("morning", "evening", "neutral"):
        profile = service.profile(snapshot["networkId"], "2025-11-01", route, phase)
        assert abs(sum(sum(p["stop"]) for p in profile.values()) - 1) < 1e-12
        assert sum(sum(p["segment"]) for p in profile.values()) > 1
    frames = [
        {
            "start": "2025-11-01T08:00:00+03:00",
            "end": "2025-11-01T09:00:00+03:00",
            "values": [{"routeId": route, "value": 100.0, "vehicleHours": None}],
        }
    ]
    result = service.sections(snapshot, frames)
    assert result and all(v["estimatedFlow"] is not None and v["rate"] is None for v in result.values())


def test_night_duty_reconciles_to_previous_service_date(tmp_path):
    path = tmp_path / "schedule.csv"
    path.write_text(
        "route;service_date;grafic;trip_num;direction;start;end;garage_number\n1;2026-09-01;0206;1;out;23:30;25:30;0042\n"
    )
    service = ScheduleService(tmp_path / "data")
    manifest = service.import_file(path)
    event = pd.DataFrame(
        {
            "route": ["1"],
            "event_time": [pd.Timestamp("2026-09-02T00:30:00+03:00")],
            "bus_exit_no": ["206"],
            "garage_number": ["42"],
        }
    )
    assert service.reconcile(manifest["id"], [event])["matched_events"] == 1


def test_operational_weather_availability_and_coverage(tmp_path):
    import pytest

    from moscowt.domain import DomainError
    from moscowt.external import OpenMeteoForecastProvider

    provider = OpenMeteoForecastProvider(tmp_path)
    payload = {
        "daily": {
            "time": ["2026-09-28"],
            "temperature_2m_mean": [8.0],
            "precipitation_sum": [3.0],
            "snowfall_sum": [0.0],
            "wind_speed_10m_mean": [4.0],
        }
    }
    manifest = provider.register(
        payload, pd.Timestamp("2026-09-27T22:00:00+03:00"), "https://api.open-meteo.com/v1/forecast"
    )
    dates = pd.Series(pd.to_datetime(["2026-09-28T00:00:00+03:00"]))
    with pytest.raises(DomainError, match="после origin"):
        provider.select(manifest["id"], dates, pd.Timestamp("2026-09-27T21:00:00+03:00"))
    assert provider.select(
        manifest["id"], dates, pd.Timestamp("2026-09-28T00:00:00+03:00")
    ).precipitation_sum.tolist() == [3.0]
    with pytest.raises(DomainError, match="не покрывает"):
        provider.select(
            manifest["id"], dates + pd.Timedelta(days=1), pd.Timestamp("2026-09-28T00:00:00+03:00")
        )


def test_segment_export_uses_same_direction_denominator_and_budget_as_map(store, tmp_path):
    import csv

    import pytest

    from moscowt.platform_exports import PeriodExportSpec

    service = SpatialService(store, tmp_path / "data")
    snapshot = store.current()
    day = "2025-11-01"
    routes = service.compile(snapshot["networkId"], day)
    route = next(iter(routes))
    path = tmp_path / "duties.csv"
    with path.open("w") as output:
        writer = csv.writer(output, delimiter=";")
        writer.writerow(
            ["route", "service_date", "grafic", "trip_num", "direction", "start", "end", "garage_number"]
        )
        for i, pattern in enumerate(routes[route]):
            writer.writerow([route, day, str(i), str(i), pattern["name"], "08:00", "09:00", str(i)])
    schedule = ScheduleService(tmp_path / "data").import_file(path)
    period = {"start": day + "T08:00:00+03:00", "end": day + "T09:00:00+03:00"}
    frame = pd.DataFrame(
        {
            "route": [route],
            "timestamp": [pd.Timestamp(period["start"])],
            "value": [100.0],
            "base_value": [100.0],
            "vehicle_hours": [4.0],
            "additional_vehicle_hours": [2.0],
        }
    )
    spec = PeriodExportSpec(
        dataset_id="test",
        forecast_id="test",
        route_ids=[route],
        time_range=period,
        metric_scope="segment",
        snapshot_id=snapshot["snapshotId"],
        schedule_id=schedule["id"],
    )
    exported = service.frame(frame, spec)
    mapped = service.sections(
        {**snapshot, "scheduleId": schedule["id"]},
        [
            {
                "start": period["start"],
                "end": period["end"],
                "values": [
                    {"routeId": route, "value": 100.0, "vehicleHours": 4.0, "additionalVehicleHours": 2.0}
                ],
            }
        ],
    )
    for row in exported.itertuples():
        assert row.value == pytest.approx(mapped[row.object_id]["estimatedFlow"])
        assert row.vehicle_hours == pytest.approx(mapped[row.object_id]["vehicleHours"])
        assert row.validations_per_vehicle_hour == pytest.approx(mapped[row.object_id]["rate"])
        assert row.denominator_method.startswith("schedule_direction")
