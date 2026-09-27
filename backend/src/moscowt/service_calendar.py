"""Published civil work calendars; a service-type proxy, not a transit timetable."""

from datetime import date

CALENDAR_SOURCE = "https://government.ru/docs/all/155500/"
EXTRA_DAYS_OFF = {
    *(date(2025, 1, day) for day in range(1, 9)),
    date(2025, 2, 23),
    date(2025, 3, 8),
    date(2025, 5, 1),
    date(2025, 5, 2),
    date(2025, 5, 8),
    date(2025, 5, 9),
    date(2025, 6, 12),
    date(2025, 6, 13),
    date(2025, 11, 3),
    date(2025, 11, 4),
    date(2025, 12, 31),
}
EXTRA_DAYS_OFF.update(
    {
        *(date(2026, 1, day) for day in range(1, 10)),
        date(2026, 2, 23),
        date(2026, 3, 8),
        date(2026, 3, 9),
        date(2026, 5, 1),
        date(2026, 5, 9),
        date(2026, 5, 11),
        date(2026, 6, 12),
        date(2026, 11, 4),
        date(2026, 12, 31),
    }
)
WORKING_WEEKENDS = {date(2025, 11, 1)}
CALENDAR_SOURCES = {2025: CALENDAR_SOURCE, 2026: "https://government.ru/docs/all/161028/"}
CALENDAR_AVAILABLE = {2025: date(2024, 10, 5), 2026: date(2025, 9, 25)}


def is_workday(day):
    return day in WORKING_WEEKENDS or (day.weekday() < 5 and day not in EXTRA_DAYS_OFF)


def profile_weekday(day, origin=None):
    """Working Saturday uses Friday; holidays on weekdays use Sunday.

    This is an explicit forecasting assumption. It does not claim that an
    operator published a Sunday timetable for that date.
    """
    if origin is not None and (
        day.year not in CALENDAR_AVAILABLE or CALENDAR_AVAILABLE[day.year] > origin.date()
    ):
        return day.weekday()
    if day in WORKING_WEEKENDS:
        return 4
    if day in EXTRA_DAYS_OFF:
        return 6
    return day.weekday()
