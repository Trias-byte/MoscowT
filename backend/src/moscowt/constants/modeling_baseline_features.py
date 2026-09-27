SPARSE = [1, 2, 3, 4, 5, 6, 7, 10, 14, 21, 28, 42, 61]

CAL = [
    "route",
    "hour",
    "dow",
    "month",
    "is_day_off",
    "is_holiday",
    "is_working_weekend",
    "profile_dow",
    "year_sin",
    "year_cos",
    "hour_sin",
    "hour_cos",
    "horizon_days",
]

HISTORY = [f"{family}_{w}" for w in [7, 14, 28, 56] for family in ["profile", "route_hour", "day_mean"]] + [
    "day_dow_28",
    "day_std_56",
    "trend_14",
    "network_trend",
    "profile_std_28",
    "profile_n_28",
    "zero_fraction_28",
    "morning_fraction_28",
    "evening_fraction_28",
]

FEATURES = CAL + HISTORY
