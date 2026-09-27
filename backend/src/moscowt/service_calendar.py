"""Published civil work calendars; a service-type proxy, not a transit timetable."""

from .constants.service_calendar import (
    CALENDAR_AVAILABLE as CALENDAR_AVAILABLE,
    CALENDAR_SOURCE as CALENDAR_SOURCE,
    CALENDAR_SOURCES as CALENDAR_SOURCES,
    EXTRA_DAYS_OFF as EXTRA_DAYS_OFF,
    WORKING_WEEKENDS as WORKING_WEEKENDS,
)


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
