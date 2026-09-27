from .service_calendar import CALENDAR_SOURCES

SOURCES = [
    {
        "id": "weather",
        "url": "https://open-meteo.com/en/docs/historical-weather-api",
        "license": "Open-Meteo CC BY 4.0 attribution; underlying ERA5 Copernicus terms",
        "access": "free noncommercial API or frozen ERA5 archive",
        "effect_status": "not_evaluated",
    },
    {
        "id": "calendar",
        "url": CALENDAR_SOURCES[2025],
        "urls": list(CALENDAR_SOURCES.values()),
        "access": "official published calendars",
        "effect_status": "not_evaluated",
    },
    {
        "id": "events",
        "url": "https://www.mosmetro.ru/news/details/7570",
        "access": "dated official announcements, curated records",
        "effect_status": "not_evaluated",
    },
    {
        "id": "traffic",
        "url": "https://transport.mos.ru/mostrans/all_news/127290",
        "access": "public aggregate announcement",
        "effect_status": "unavailable",
        "limitation": "No downloadable route-hour historical speeds confirmed; no traffic values are fabricated",
    },
    {
        "id": "accidents",
        "url": "https://dtp-stat.ru/opendata/",
        "license": "Использование материалов с активной ссылкой на https://dtp-stat.ru/",
        "access": "https://dtp-stat.ru/media/opendata/moskva.geojson.zip; filter Moscow events in 2025",
        "effect_status": "not_evaluated",
        "limitation": "Spatial proximity only; no publication time or disruption duration. Mostly injury crashes, not a congestion time series.",
    },
]

EVENTS = [
    {
        "id": "restoration-2025-08-11",
        "route_ids": ["7", "50"],
        "start": "2025-08-11",
        "end": "2025-09-10",
        "published_at": "2025-08-12T00:00:00+03:00",
        "url": "https://www.mosmetro.ru/news/details/7570",
    },
    {
        "id": "new-stops-2025-09-10",
        "route_ids": ["7", "50"],
        "start": "2025-09-10",
        "end": "2026-01-01",
        "published_at": "2025-09-11T00:00:00+03:00",
        "url": "https://www.mosmetro.ru/news/details/7763",
    },
]

WEATHER_FIELDS = ("temperature_2m_mean", "precipitation_sum", "snowfall_sum", "wind_speed_10m_mean")
