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

CALENDAR_SOURCES = {2025: CALENDAR_SOURCE, 2026: "https://government.ru/docs/all/161028/"}

CALENDAR_AVAILABLE = {2025: date(2024, 10, 5), 2026: date(2025, 9, 25)}

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
