"""
Генерация submission.csv v3 с тройным ансамблем:
CatBoost (0.7) + LightGBM (0.1) + Random Forest (0.2).

Включает локальный замер WAPE на октябре (до и после постобработки).
Платформенный результат (справочно): ~0.859-0.861.
"""

import numpy as np
import pandas as pd
import lightgbm as lgb
from catboost import CatBoostRegressor
from sklearn.ensemble import RandomForestRegressor
from pathlib import Path
from holidays import Russia
import warnings
warnings.filterwarnings("ignore")

DATA_DIR = Path("dataset")

# Веса ансамбля
W_LGB = 0.1
W_CAT = 0.7
W_RF  = 0.2
assert abs(W_LGB + W_CAT + W_RF - 1.0) < 1e-6, "Веса должны суммироваться в 1.0"

# ============================================================
# 1. Загрузка
# ============================================================
print("=" * 60)
print("ЗАГРУЗКА ДАННЫХ")
print("=" * 60)

df = pd.read_parquet(DATA_DIR / "features_v4.parquet")
df["date"] = pd.to_datetime(df["date"])
df["route"] = df["route"].astype(int)
df["hour"] = df["hour"].astype(int)
df = df.sort_values(["route", "date", "hour"]).reset_index(drop=True)
df = df.fillna(0)

df_train = df[df["route"] != 5].copy()
print(f"Строк всего: {len(df):,}")
print(f"Строк без маршрута 5: {len(df_train):,}")

# ============================================================
# 2. Признаки
# ============================================================
FEATURES = [
    "route", "hour", "dow", "month", "week_of_year",
    "is_weekend", "is_saturday", "is_sunday",
    "is_holiday", "is_new_year_holidays",
    "is_december", "is_november", "is_school_holiday",
    "is_night", "is_night_deep", "is_new_year_eve", "is_school_start",
    "holiday_streak", "days_after_holiday",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "lag_24h", "lag_168h", "lag_336h", "lag_672h",
    "roll_mean_7", "roll_std_7",
    "roll_mean_14", "roll_std_14",
    "roll_mean_28", "roll_std_28",
    "route_mean_30",
]
TARGET = "boardings"

print(f"Признаков: {len(FEATURES)}")


# ============================================================
# 3. ФУНКЦИЯ ПОСТОБРАБОТКИ (единая для local и final)
# ============================================================
def apply_postprocessing(submission):
    """
    Применяет постобработку:
    - маршрут 5 = 0
    - обрезка часов 2-3 до 2
    - обрезка часов 1 и 4 до 150
    - фикс 3 ноября (замена на среднее 1-2 ноября)
    """
    submission = submission.copy()

    # Маршрут 5 = 0
    submission.loc[submission["route"] == 5, "prediction"] = 0

    # Ночные часы
    mask_nd = submission["hour"].isin([2, 3])
    submission.loc[mask_nd, "prediction"] = np.minimum(
        submission.loc[mask_nd, "prediction"], 2
    )

    mask_n14 = submission["hour"].isin([1, 4])
    submission.loc[mask_n14, "prediction"] = np.minimum(
        submission.loc[mask_n14, "prediction"], 150
    )

    # Фикс 3 ноября (только если есть в данных)
    if pd.Timestamp("2025-11-03") in submission["date"].values:
        mask_3nov = submission["date"] == pd.Timestamp("2025-11-03")
        ref_3nov = submission[submission["date"].isin(
            [pd.Timestamp("2025-11-01"), pd.Timestamp("2025-11-02")]
        )].groupby(["route", "hour"])["prediction"].mean()

        for (r, h), base in ref_3nov.items():
            m = mask_3nov & (submission["route"] == r) & (submission["hour"] == h)
            submission.loc[m, "prediction"] = base

    return submission


# ============================================================
# 4. ЛОКАЛЬНЫЙ ЗАМЕР WAPE НА ОКТЯБРЕ
# ============================================================
print("\n" + "=" * 60)
print("ЛОКАЛЬНЫЙ ЗАМЕР WAPE НА ОКТЯБРЕ")
print("=" * 60)

VALID_START = pd.Timestamp("2025-10-01")

train_local = df_train[df_train["date"] < VALID_START].copy()
valid_local = df_train[df_train["date"] >= VALID_START].copy()

print(f"Train (янв-сен): {len(train_local):,} строк")
print(f"Valid (окт):     {len(valid_local):,} строк")

X_train = train_local[FEATURES]
y_train = train_local[TARGET]
X_valid = valid_local[FEATURES]
y_valid = valid_local[TARGET]

w_train = np.where(train_local["is_night_deep"] == 1, 0.3, 1.0)
w_valid = np.where(valid_local["is_night_deep"] == 1, 0.3, 1.0)

# --- LightGBM (local) ---
print("\nОбучение LightGBM (local)...")
model_lgb_local = lgb.LGBMRegressor(
    n_estimators=3000,
    learning_rate=0.03,
    num_leaves=63,
    min_child_samples=20,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_alpha=0.1,
    reg_lambda=0.1,
    objective="regression",
    metric="mae",
    random_state=42,
    n_jobs=-1,
    verbose=-1,
)
model_lgb_local.fit(
    X_train, y_train,
    sample_weight=w_train,
    eval_set=[(X_valid, y_valid)],
    eval_sample_weight=[w_valid],
    eval_metric="mae",
    callbacks=[
        lgb.early_stopping(100, verbose=False),
        lgb.log_evaluation(period=500),
    ],
)
pred_lgb_local = np.maximum(model_lgb_local.predict(X_valid), 0)
wape_lgb = np.abs(y_valid - pred_lgb_local).sum() / y_valid.sum()
print(f"  LightGBM WAPE-score: {1 - wape_lgb:.4f}")

# --- CatBoost (local) ---
print("\nОбучение CatBoost (local)...")
model_cat_local = CatBoostRegressor(
    iterations=3000,
    learning_rate=0.03,
    depth=8,
    l2_leaf_reg=3.0,
    loss_function="MAE",
    eval_metric="MAE",
    random_seed=42,
    verbose=500,
    early_stopping_rounds=100,
    thread_count=-1,
)
model_cat_local.fit(
    X_train, y_train,
    sample_weight=w_train,
    eval_set=(X_valid, y_valid),
    use_best_model=True,
)
pred_cat_local = np.maximum(model_cat_local.predict(X_valid), 0)
wape_cat = np.abs(y_valid - pred_cat_local).sum() / y_valid.sum()
print(f"  CatBoost WAPE-score: {1 - wape_cat:.4f}")

# --- Random Forest (local) ---
print("\nОбучение Random Forest (local)...")
model_rf_local = RandomForestRegressor(
    n_estimators=300,
    max_depth=20,
    min_samples_leaf=5,
    max_features=0.5,
    n_jobs=-1,
    random_state=42,
    verbose=0,
)
model_rf_local.fit(X_train, y_train, sample_weight=w_train)
pred_rf_local = np.maximum(model_rf_local.predict(X_valid), 0)
wape_rf = np.abs(y_valid - pred_rf_local).sum() / y_valid.sum()
print(f"  Random Forest WAPE-score: {1 - wape_rf:.4f}")

# --- Ансамбль (local, ДО постобработки) ---
pred_local_raw = (
    W_LGB * pred_lgb_local
    + W_CAT * pred_cat_local
    + W_RF  * pred_rf_local
)
wape_local_raw = np.abs(y_valid - pred_local_raw).sum() / y_valid.sum()
print(f"\n--- Ансамбль ДО постобработки ---")
print(f"  Веса: LGBM={W_LGB}, CatBoost={W_CAT}, RF={W_RF}")
print(f"  WAPE-score: {1 - wape_local_raw:.4f}")
print(f"  MAE:        {np.abs(y_valid - pred_local_raw).mean():.2f}")

# --- Постобработка ---
valid_sub = valid_local[["route", "date", "hour"]].copy()
valid_sub["prediction"] = pred_local_raw
valid_sub_pp = apply_postprocessing(valid_sub)

pred_local_pp = valid_sub_pp["prediction"].values
wape_local_pp = np.abs(y_valid - pred_local_pp).sum() / y_valid.sum()
print(f"\n--- Ансамбль ПОСЛЕ постобработки ---")
print(f"  WAPE-score: {1 - wape_local_pp:.4f}")
print(f"  MAE:        {np.abs(y_valid - pred_local_pp).mean():.2f}")
print(f"\nВлияние постобработки: {(1 - wape_local_pp) - (1 - wape_local_raw):+.4f}")

# WAPE по маршрутам (после постобработки)
valid_local_pp = valid_local.copy()
valid_local_pp["pred"] = pred_local_pp
route_wape = valid_local_pp.groupby("route").apply(
    lambda g: 1 - np.abs(g[TARGET] - g["pred"]).sum() / g[TARGET].sum()
).round(4)
print(f"\nWAPE-score по маршрутам:")
print(route_wape.to_string())

# WAPE по часам (после постобработки)
hour_wape = valid_local_pp.groupby("hour").apply(
    lambda g: 1 - np.abs(g[TARGET] - g["pred"]).sum() / max(g[TARGET].sum(), 1)
).round(4)
print(f"\nWAPE-score по часам:")
print(hour_wape.to_string())


# ============================================================
# 5. ОБУЧЕНИЕ НА ВСЕХ ДАННЫХ
# ============================================================
print("\n" + "=" * 60)
print("ОБУЧЕНИЕ НА ВСЕХ ДАННЫХ (янв-окт)")
print("=" * 60)

w_full = np.where(df_train["is_night_deep"] == 1, 0.3, 1.0)

# --- LightGBM (full) ---
print("\nLightGBM (full)...")
model_lgb = lgb.LGBMRegressor(
    n_estimators=332,
    learning_rate=0.03,
    num_leaves=63,
    min_child_samples=20,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_alpha=0.1,
    reg_lambda=0.1,
    objective="regression",
    metric="mae",
    random_state=42,
    n_jobs=-1,
    verbose=-1,
)
model_lgb.fit(df_train[FEATURES], df_train[TARGET], sample_weight=w_full)
print("  LightGBM обучен")

# --- CatBoost (full) ---
print("\nCatBoost (full)...")
model_cat = CatBoostRegressor(
    iterations=1378,
    learning_rate=0.03,
    depth=8,
    l2_leaf_reg=3.0,
    loss_function="MAE",
    random_seed=42,
    verbose=200,
    thread_count=-1,
)
model_cat.fit(df_train[FEATURES], df_train[TARGET], sample_weight=w_full)
print("  CatBoost обучен")

# --- Random Forest (full) ---
print("\nRandom Forest (full)...")
model_rf = RandomForestRegressor(
    n_estimators=300,
    max_depth=20,
    min_samples_leaf=5,
    max_features=0.5,
    n_jobs=-1,
    random_state=42,
    verbose=0,
)
model_rf.fit(df_train[FEATURES], df_train[TARGET], sample_weight=w_full)
print("  Random Forest обучен")


# ============================================================
# 6. Построение сетки на ноя-дек
# ============================================================
print("\n" + "=" * 60)
print("ПОСТРОЕНИЕ СЕТКИ НА НОЯ-ДЕК")
print("=" * 60)

ALL_ROUTES = [1, 5, 7, 11, 12, 17, 25, 26, 28, 50]
future_dates = pd.date_range("2025-11-01", "2025-12-31", freq="D")
hours = list(range(24))

future = pd.MultiIndex.from_product(
    [ALL_ROUTES, future_dates, hours], names=["route", "date", "hour"]
).to_frame(index=False)

future["dow"] = future["date"].dt.dayofweek
future["month"] = future["date"].dt.month
future["day"] = future["date"].dt.day
future["week_of_year"] = future["date"].dt.isocalendar().week.astype(int)
future["is_weekend"] = (future["dow"] >= 5).astype(int)
future["is_saturday"] = (future["dow"] == 5).astype(int)
future["is_sunday"] = (future["dow"] == 6).astype(int)

ru_holidays = Russia(years=[2025, 2026])
holiday_dates = set(ru_holidays.keys())
future["is_holiday"] = future["date"].dt.date.isin(holiday_dates).astype(int)
future["is_new_year_holidays"] = (
    (future["date"].dt.month == 1) & (future["date"].dt.day <= 8)
).astype(int)
future["is_december"] = (future["date"].dt.month == 12).astype(int)
future["is_november"] = (future["date"].dt.month == 11).astype(int)

future["is_school_holiday"] = 0
future.loc[(future["date"].dt.month == 12) & (future["date"].dt.day >= 28),
           "is_school_holiday"] = 1

future["is_night"] = future["hour"].isin([0, 1, 2, 3, 4]).astype(int)
future["is_night_deep"] = future["hour"].isin([2, 3]).astype(int)
future["is_new_year_eve"] = ((future["month"] == 12) & (future["day"] == 31)).astype(int)
future["is_school_start"] = 0

future["hour_sin"] = np.sin(2 * np.pi * future["hour"] / 24)
future["hour_cos"] = np.cos(2 * np.pi * future["hour"] / 24)
future["dow_sin"] = np.sin(2 * np.pi * future["dow"] / 7)
future["dow_cos"] = np.cos(2 * np.pi * future["dow"] / 7)

# holiday_streak / days_after_holiday
dates_df = pd.DataFrame({
    "date": pd.date_range(future["date"].min(), future["date"].max(), freq="D")
})
dates_df["is_weekend"] = (dates_df["date"].dt.dayofweek >= 5).astype(int)
dates_df["is_holiday"] = dates_df["date"].dt.date.isin(holiday_dates).astype(int)
dates_df["is_day_off"] = (
    (dates_df["is_weekend"] == 1) | (dates_df["is_holiday"] == 1)
).astype(int)
dates_df["holiday_streak"] = dates_df.groupby(
    (dates_df["is_day_off"] != dates_df["is_day_off"].shift()).cumsum()
)["is_day_off"].cumsum() * dates_df["is_day_off"]
dates_df["days_after_holiday"] = dates_df.groupby(
    (dates_df["is_day_off"] == 0).cumsum()
).cumcount()

future = future.merge(
    dates_df[["date", "holiday_streak", "days_after_holiday"]],
    on="date", how="left",
)

# --- Лаги из октября ---
print("\nСчитаем лаги для future...")
hist = df_train[["route", "date", "hour", TARGET]].copy()
hist_dict = {}
for row in hist.itertuples(index=False):
    hist_dict[(row.route, row.hour, row.date)] = row.boardings

october_dates = pd.date_range("2025-10-01", "2025-10-31", freq="D")

def find_october_analog(date):
    dow = date.dayofweek
    candidates = [d for d in october_dates if d.dayofweek == dow]
    if not candidates:
        return october_dates[-1]
    return min(candidates, key=lambda d: abs(d.day - date.day))

future["october_analog"] = future["date"].apply(find_october_analog)

def get_lag(route, hour, date, lag_days):
    return hist_dict.get((route, hour, date - pd.Timedelta(days=lag_days)), np.nan)

future["lag_24h"] = future.apply(
    lambda r: get_lag(r["route"], r["hour"], r["october_analog"], 1), axis=1
)
future["lag_168h"] = future.apply(
    lambda r: get_lag(r["route"], r["hour"], r["october_analog"], 7), axis=1
)
future["lag_336h"] = future.apply(
    lambda r: get_lag(r["route"], r["hour"], r["october_analog"], 14), axis=1
)
future["lag_672h"] = future.apply(
    lambda r: get_lag(r["route"], r["hour"], r["october_analog"], 28), axis=1
)

def get_rolling(route, hour, end_date, window, fn="mean"):
    vals = []
    for k in range(1, window + 1):
        v = hist_dict.get((route, hour, end_date - pd.Timedelta(days=k)))
        if v is not None:
            vals.append(v)
    if not vals:
        return np.nan
    return np.mean(vals) if fn == "mean" else np.std(vals)

for window in [7, 14, 28]:
    future[f"roll_mean_{window}"] = future.apply(
        lambda r: get_rolling(r["route"], r["hour"], r["october_analog"], window, "mean"),
        axis=1,
    )
    future[f"roll_std_{window}"] = future.apply(
        lambda r: get_rolling(r["route"], r["hour"], r["october_analog"], window, "std"),
        axis=1,
    )

route_means = df_train.groupby("route")[TARGET].mean()
future["route_mean_30"] = future["route"].map(route_means)


# ============================================================
# 7. Прогноз — тройной ансамбль
# ============================================================
print("\n" + "=" * 60)
print("ПРОГНОЗ (ТРОЙНОЙ АНСАМБЛЬ)")
print("=" * 60)

future = future.fillna(0)

pred_lgb = np.maximum(model_lgb.predict(future[FEATURES]), 0)
pred_cat = np.maximum(model_cat.predict(future[FEATURES]), 0)
pred_rf  = np.maximum(model_rf.predict(future[FEATURES]), 0)

future["prediction"] = W_LGB * pred_lgb + W_CAT * pred_cat + W_RF * pred_rf
print(f"Веса: LGBM={W_LGB}, CatBoost={W_CAT}, RF={W_RF}")


# ============================================================
# 8. Постобработка
# ============================================================
print("\nПостобработка...")
submission = future[["route", "date", "hour", "prediction"]].copy()
submission = apply_postprocessing(submission)
submission["prediction"] = submission["prediction"].round().astype(int)


# ============================================================
# 9. Сохранение
# ============================================================
submission = submission.sort_values(["route", "date", "hour"]).reset_index(drop=True)
submission.to_csv(DATA_DIR / "submission.csv", sep=";", index=False, encoding="utf-8")

print("\n" + "=" * 60)
print("СОХРАНЕНИЕ")
print("=" * 60)
print(f"Файл: {DATA_DIR / 'submission.csv'}")
print(f"Строк: {len(submission):,} (ожидалось 14 640)")

print(f"\nСтатистика по маршрутам:")
print(submission.groupby("route")["prediction"].agg(
    ["mean", "min", "max", "sum"]
).to_string())

print(f"\nПроверка 3 ноября:")
print(f"  1 ноября: {submission[submission['date'] == '2025-11-01']['prediction'].mean():.0f}")
print(f"  2 ноября: {submission[submission['date'] == '2025-11-02']['prediction'].mean():.0f}")
print(f"  3 ноября: {submission[submission['date'] == '2025-11-03']['prediction'].mean():.0f}")


# ============================================================
# 10. ИТОГОВЫЙ ОТЧЁТ
# ============================================================
print("\n" + "=" * 60)
print("ИТОГОВЫЙ ОТЧЁТ")
print("=" * 60)
print(f"Локальный WAPE (окт, БЕЗ постобработки): {1 - wape_local_raw:.4f}")
print(f"Локальный WAPE (окт, С постобработкой):  {1 - wape_local_pp:.4f}")
print(f"Влияние постобработки:                   {(1 - wape_local_pp) - (1 - wape_local_raw):+.4f}")
print(f"\nАнсамбль: LGBM ({W_LGB}) + CatBoost ({W_CAT}) + RF ({W_RF})")