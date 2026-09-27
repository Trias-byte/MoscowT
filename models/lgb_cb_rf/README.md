# Исходный ансамбль LGB/CB/RF

`vendor/original.py` — неизменённая копия `06_submission_LGB_CB_RF.py` из локальной поставки пользователя `LGB_CB_RF_ensemble/LGB_CB_RF_ensemble`.

SHA-256: `ac131522b088e8b8b4dc8968894838f7b94818fdf7df43cddb22e69008e502dc`.

Веса: CatBoost 0,7; LightGBM 0,1; Random Forest 0,2. Исходник обучает модели при запуске и записывает `dataset/submission.csv`. Его нельзя импортировать в HTTP-процесс. Входной `features_v4.parquet` и эталонный `submission.csv` относятся к данным, а не к исходному коду.

## Воспроизведение

Из корня репозитория (исходная поставка находится в соседней директории):

```bash
docker build --network=host -f models/lgb_cb_rf/Dockerfile -t moscowt-models:development .
mkdir -p artifacts-data/legacy-reproduction/dataset
docker run --rm --cpus=2 --memory=6g --user "$(id -u):$(id -g)" \
  -v "$PWD/artifacts-data/legacy-reproduction:/output" \
  -v "$PWD/../LGB_CB_RF_ensemble/LGB_CB_RF_ensemble/features_v4.parquet:/output/dataset/features_v4.parquet:ro" \
  -v "$PWD/../LGB_CB_RF_ensemble/LGB_CB_RF_ensemble/submission.csv:/reference.csv:ro" \
  moscowt-models:development
```

Важно: монтируются отдельные входные файлы, а каталог вывода остаётся записываемым. Wrapper сохраняет обученные estimators в `legacy_models.joblib`, результат в `dataset/submission.csv`, длительность, RSS, версии библиотек, checksum и сравнение — в `reproduction.json`.

Фактический прогон 26.09.2026: 102,69 с, RSS 1 189 036 032 байта. Ключи совпали; значения различаются в 12 038 строках, максимальная абсолютная разность 714, суммы 12 438 321 против 12 102 911. [Отчёт](../../docs/ensemble-reproduction.json). Это успешно выполненный исходник, **не доказательство точного числового воспроизведения** эталона. Окружение автора не было приложено; причина расхождения не установлена. Конкурсные исключения (включая № 5 и ночные часы) остаются в неизменённом исходнике и не перенесены в общий сервис.
