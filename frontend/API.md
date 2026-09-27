# HTTP API

Основной сервис — `/api/v2`, переходный контракт карты — `/api/v1` (`contractVersion: 2`). Pydantic-запросы отклоняют неизвестные поля. [OpenAPI](../backend/openapi.json) генерируется командой `moscowt openapi`; живая схема — `/docs` и `/openapi.json`.

## Время и смысл чисел

Интервалы полуоткрытые `[start,end)`, границы — часы с явным часовым поясом, приводятся к `Europe/Moscow`. `origin` — момент выпуска, `created_at` — время фактического расчёта. Прогнозируемое окно начинается не раньше origin; обучение и признаки не читают будущие target. Для `grain=day` нужны полуночи, для `month` — первые числа месяцев.

Маршрутная цель — `successful_validations`, успешные валидации. Пропуск — `null`, ноль — известное значение. Неполный день/месяц не превращается в известную сумму. `auto` выбирает факт при его наличии, иначе прогноз. Интенсивность — отношение сумм, не среднее почасовых отношений.

## Версии, импорт и фоновые задания

| Метод                                                   | Назначение                                                                   |
| ------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `GET /capabilities`                                     | Текущие dataset/snapshot, ограничения, шкала 5/20/34/50                      |
| `POST /blobs`                                           | Потоковое тело CSV `application/octet-stream`, до 12 ГиБ, ответ `{id,bytes}` |
| `POST /uploads`                                         | `{blob_id,spec}` → задание предварительной проверки                          |
| `GET /uploads/{id}`                                     | Отчёт preview, конфликты, полнота, базовая версия                            |
| `POST /uploads/{id}/apply`                              | Создать и атомарно активировать новую версию                                 |
| `GET /datasets`, `GET /datasets/{id}`                   | Версии, покрытие, суммы, зависимости моделей                                 |
| `POST /datasets/{id}/activate`                          | Откат/переключение каталога; старые выпуски сохраняются                      |
| `GET /routes`, `GET /routes/{id}`, `POST /routes`       | Реестр и версия описания маршрута                                            |
| `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel` | Задания и отмена                                                             |
| `GET /jobs/{id}/download`                               | Скачать готовый экспорт                                                      |

Пример `spec` импорта:

```json
{
  "kind": "labels",
  "mode": "append",
  "route_ids": ["17"],
  "complete": false,
  "time_range": { "start": "2025-09-01T00:00:00+03:00", "end": "2025-10-01T00:00:00+03:00" }
}
```

`kind`: labels/events; `mode`: append/upsert/replace. Файл labels: `route;date;hour;boardings`. Полные форматы — [backend](../backend/README.md). У заданий обязателен `Idempotency-Key` (1–128 символов). Повтор ключа с другим телом — 409, переполненная очередь — 429. Статусы: pending/running/ready/failed/cancelled, результат содержится в `result`. Все клиентские периоды независимы; поллинг заданий продолжается после сворачивания панели.

## Модели, прогноз, публикация

`GET /model-types`, `GET /models`, `GET /model-evaluations` возвращают адаптеры, паспорта и проверки. В паспорте — версия данных, рецепт, параметры/seed, внешние источники, код, библиотеки, checksum артефакта, длительность, покрытие по маршрутам, минимальная история и максимальный горизонт.

```http
POST /api/v2/training-runs
Idempotency-Key: train-example
Content-Type: application/json
```

```json
{
  "dataset_id": "DATASET_ID",
  "model_type": "lgb_cb_rf",
  "route_ids": ["17"],
  "time_range": { "start": "2025-01-01T00:00:00+03:00", "end": "2025-10-01T00:00:00+03:00" },
  "feature_groups": ["calendar"],
  "parameters": {}
}
```

Типы: `seasonal`, `lgb_cb_rf`, `annual_scenario`. `weather/events` требуют `external_snapshot_id` и ансамбль. `POST /forecast-runs` принимает:

```json
{
  "dataset_id": "DATASET_ID",
  "model_id": "MODEL_ID",
  "route_ids": ["17"],
  "origin": "2025-10-01T00:00:00+03:00",
  "time_range": { "start": "2025-10-01T00:00:00+03:00", "end": "2025-11-01T00:00:00+03:00" }
}
```

`weather_forecast_id` необязателен: только суточный выпуск модели с погодными признаками, полученный до origin и покрывающий окно. `GET /forecast-runs`, `GET /forecast-runs/{id}` читают immutable выпуски. `GET /forecast-runs/{id}/evaluation?truth_dataset_id=...` сравнивает с фактами; неизвестная истинная целевая сумма не становится нулём.

`POST /publications` принимает `{forecast_id,expected_snapshot_id?,schedule_id?,schedule_scenario?}`. Проверяет данные и переключает v1-карту атомарно. Обучение само карту не меняет. При исправлении истории требуется явное переобучение; смешивание model/dataset разных версий отклоняется.

## Запрос и выгрузка чисел

`POST /forecasts/query` и `POST /exports` используют одну спецификацию:

```json
{
  "dataset_id": "DATASET_ID",
  "forecast_id": "FORECAST_ID",
  "route_ids": ["17"],
  "time_range": { "start": "2025-11-01T08:00:00+03:00", "end": "2025-11-01T09:00:00+03:00" },
  "mode": "forecast",
  "grain": "hour",
  "metric_scope": "route",
  "format": "csv",
  "scenario_id": null,
  "schedule_id": null
}
```

Ответ запроса — `{spec,rows}`. Строки содержат базовое/скорректированное значение, источник, dataset/model/forecast/origin, вагоно-часы и метод их оценки, коэффициент и сценарий. `grain`: hour/day/month. `metric_scope`: route/stop/segment; для двух последних нужен `snapshot_id` геометрии, опционально `object_ids`. Без полной географии выбранных маршрутов и дат пространственный экспорт отклоняется. Посадки по остановкам сохраняют маршрутную сумму; потоки последовательных участков неаддитивны. Метрики/методы явно помечены как оценки, а не наблюдения.

Упрощённый `GET /forecasts?forecast_id=...&start=...&end=...&route_ids=17` возвращает маршрутные часы. Экспорт: CSV UTF-8 BOM `;`, Parquet — ZIP с `route_hours.parquet` и `manifest.json`. Сохранённые модельные значения не округляются; конкурсный CSV использует `floor(value+0.5)`.

`POST /competition-exports` принимает только `{forecast_id}`: сервер выбирает исходный шаблон, origin 01.11.2025 и полный исходный период/маршруты; фильтры и ручные сценарии не входят. `POST /bundles` создаёт полный переносимый архив, включая все существующие версии в данном сервисе.

## Сценарии, расписания, источники

`POST /scenarios`:

```json
{
  "forecast_id": "FORECAST_ID",
  "route_ids": ["17"],
  "time_range": { "start": "2025-11-05T08:00:00+03:00", "end": "2025-11-05T09:00:00+03:00" },
  "coefficients": { "weather": 1, "event": 1.2, "season": 1 },
  "additional_vehicle_hours": 2
}
```

Коэффициенты 0…2, шаг 0,05, нейтральное значение 1. Сценарий неизменяем, ID определяется содержимым, доступен через `GET /scenarios/{id}`. Поправки меняют только прогноз, не факты. Бюджет делится по полному набору маршрутов и часов сценария, поэтому выбор части периода не меняет распределение. Без известного исходного знаменателя дополнительные часы не создают фиктивную интенсивность.

`POST /schedules` принимает `{blob_id}` и ставит импорт в очередь. `GET /schedules` показывает происхождение и применимость; `/schedules/{id}/reconciliation?dataset_id=...` проверяет связь с событиями. `schedule_scenario=true` разрешает перенос остановочного недельного расписания за пределы сентября 2026. Полные назначения не переносятся как недельный шаблон.

`GET /external-sources`, `GET /weather-forecasts`, `POST /weather-forecasts`: происхождение/качество и отдельная загрузка актуального бесплатного прогноза. Сеть не нужна при чтении сохранённых результатов. [Ссылки и эксперимент](../docs/EXTERNAL_SOURCES.md).

## Переходный `/api/v1`

Карта использует `GET /capabilities`, `/network?date=YYYY-MM-DD`, `/routes` и `POST /map-snapshot`, `/timeseries`, `/route-comparison`, `/heatmap`. Область:

```json
{
  "snapshotId": "SNAPSHOT_ID",
  "routeIds": ["17"],
  "timeRange": { "start": "2025-11-01T00:00:00+03:00", "end": "2025-11-02T00:00:00+03:00" },
  "mode": "auto",
  "grain": "hour",
  "scenarioId": null
}
```

`geometry` может фильтровать видимость/участок, но не маршрутные суммы; `metricScope` здесь всегда route. Сценарные участки той же области рассчитывает `POST /api/v2/sections`. ID снимка и сценария входят в ключи кэша; числовое представление у двух API общее. Старые `/exports`, `/submissions`, `/jobs/{id}` сохраняют прежний протокол карты. Географический CSV импортируется через существующий `/route-imports` и не создаёт автоматически числовые факты.

Ошибки предметной области: `{error:{code,message}}`; ошибки схемы — стандартный HTTP 422 с `detail`. Внешний запуск без аутентификации не предусмотрен: Compose привязывает порт к localhost.

## Сценарии с пересчётом и перенос моделей

Все пути ниже имеют префикс `/api/v2`; фоновые операции требуют `Idempotency-Key` и возвращают задание. `/jobs/{id}` отслеживает выполнение, `/jobs/{id}/download` выдаёт файл готовой выгрузки.

| Метод / путь                                    | Назначение                                                                                                                     |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `POST /uploads`                                 | В `spec`: `new_dataset=true`, `dataset_name` для самостоятельного набора; либо `base_dataset_id` для новой редакции выбранного |
| `POST /blobs?kind=model`                        | Поток ZIP, максимум 1 ГиБ; CSV остаётся `kind=data`                                                                            |
| `POST /models/import-preview`, `/models/import` | `{blob_id}`: проверка пакета / фоновый импорт                                                                                  |
| `POST /models/{id}/export`                      | Переносимый ZIP модели                                                                                                         |
| `POST /models/{id}/weights?forecast_id=…`       | `{catboost:0.7,lightgbm:0.1,random_forest:0.2}`; новая модель, опциональный пересчёт указанного выпуска                        |
| `GET /factor-datasets`, `POST /factor-datasets` | Сохранённые источники; получение `{kind:weather,start,end}` или `{kind:accidents,network_id}`                                  |
| `GET /accidents`                                | Исторические точки с фильтром по `dataset_id`, `start`, `end`, `west/south/east/north`                                         |
| `GET /accident-candidates`                      | `longitude`, `latitude`, `date`, `snapshot_id`; возможные маршруты в 100 м                                                     |
| `POST /forecast-runs/{id}/weather-profile`      | `{start,end}` внутри выпуска; минимум/среднее/максимум исходных почасовых погодных входов                                      |
| `POST /scenarios`                               | Создание неизменяемого черновика с `engine=recompute`                                                                          |
| `POST /scenarios/{id}/runs`                     | Фоновый расчёт; завершённый `job.result.id` отличается от ID черновика                                                         |
| `GET /scenarios/{id}`                           | Черновик либо завершённый результат с lineage, итогами и чувствительностью; незавершённый результат недоступен                 |

Сценарий содержит `forecast_id`, `route_ids`, `time_range`, `weather` (temperature_2m / relative_humidity_2m / precipitation, `null` сохраняет профиль), `coefficients`, `incidents`, `schedule` и `additional_vehicle_hours`. ДТП задаёт `id`, `longitude`, `latitude`, подтверждённые `route_ids`, `start` с часовым поясом, `duration_minutes` и `reduction` 0…1. Расписание: `base_schedule_id` или `base_headway_minutes`, `schedule_id` либо `headway_minutes`, границы `service_start_minute`/`service_end_minute`, список `departures`, `elasticity` 0…1 и явный `allow_period_reuse`.

Для карты передавайте завершённый `scenarioId` в v1/v2 Scope; для `/forecasts/query` и `/exports` — `scenario_id`. Базовый выпуск сохраняется. `/api/v1` и старые коэффициентные сценарии совместимы. Старый `engine=legacy` не принимает физические погодные поля/ДТП/расписание.

`TrainingSpec` поддерживает `weather_hourly_id`, `accident_links_id`, `feature_groups` и `purpose=research` для исследовательских артефактов. `ForecastSpec.diagnostic_observed_factors=true` допускает факты окна только в маркированном исследовании; такой выпуск нельзя опубликовать или экспортировать конкурсным профилем.

## Годовой выпуск в календаре

`GET /api/v1/capabilities` возвращает `forecastOptions`: основной выпуск и готовые годовые сценарии для тех же данных, маршрутов и момента выпуска. `start` включён, `end` исключён; `kind=annual_scenario` и `qualityNote` сохраняют отметку о невалидированном горизонте.

`POST /api/v2/forecast-map-views` принимает `forecast_id` и `snapshot_id`, проверяет совпадение набора данных и возвращает неизменяемый снимок для просмотра. Общая публикация не переключается. Запросы карты используют возвращённый `snapshotId`, экспорт — выбранный `forecast_id`. В демонстрации доступен период до 31.10.2026 включительно, ровно год от 01.11.2025. За его пределами требуется новый выпуск; продолжение исторической геометрии не предполагается.
