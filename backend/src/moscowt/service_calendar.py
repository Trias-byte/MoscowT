"""2025 civil work calendar; a proxy for service type, not a transit timetable."""

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
WORKING_WEEKENDS = {date(2025, 11, 1)}


def is_workday(day):
    return day in WORKING_WEEKENDS or (day.weekday() < 5 and day not in EXTRA_DAYS_OFF)


def profile_weekday(day):
    """Working Saturday uses Friday; holidays on weekdays use Sunday.

    This is an explicit forecasting assumption. It does not claim that an
    operator published a Sunday timetable for that date.
    """
    if day in WORKING_WEEKENDS:
        return 4
    if day in EXTRA_DAYS_OFF:
        return 6
    return day.weekday()
