from datetime import datetime
from zoneinfo import ZoneInfo

ROUTES = (1, 5, 7, 11, 12, 17, 25, 26, 28, 50)

TZ = ZoneInfo("Europe/Moscow")

HISTORY_START = datetime(2025, 1, 1, tzinfo=TZ)

HISTORY_END = datetime(2025, 11, 1, tzinfo=TZ)

FINAL_END = datetime(2026, 1, 1, tzinfo=TZ)

METRIC = "successful_validations"
