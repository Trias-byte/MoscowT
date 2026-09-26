# HTTP-контракт frontend ↔ backend, версия 2

Префикс `/api/v1`, `contractVersion: 2`. Реализация контрактов: Pydantic в `backend/src/moscowt/domain.py` и `wire.py`, Zod в `frontend/src/lib/contracts.ts`. Машинная схема: `backend/openapi.json`, `/openapi.json`, `/docs`.

Совместимость с остановочным демоконтрактом v1 не поддерживается. Имя показателя — `successful_validations`, детализация — `route`, единица — `validations`; число обозначает сумму **за указанный интервал**. Для карты `intervalUnit` уточняет `validations/hour` либо `validations/day`.

## Инициализация

1. `GET /capabilities` возвращает текущий `snapshotId`, независимый `networkSnapshotId`, возможности, список целевых маршрутов, `historyRange` и `forecastRange`.
2. `GET /network?snapshotId=...` возвращает справочник, маршруты, шаблоны, вхождения остановок, перегоны и сведения о происхождении. `GET /routes?snapshotId=...` отдельно возвращает реестр маршрутов.
3. Все запросы выбранного просмотра передают один явный `snapshotId`. Сервер не подменяет неизвестный ID текущим.

Реестр содержит 10 целевых и 5 дополнительных справочных маршрутов. У маршрута независимые `isTarget`, `hasData`, `hasGeometry`, `historySupport`. Новая геометрия имеет `geometryQuality=mapped`: линии следуют узлам путей OSM; legacy-снимки сохраняют `schematic`. `stationId` — исходная позиция остановки, `id` — вхождение в направление. `pattern-osm-<relation_id>` связывает направление с OSM relation; маршрут, направление и остановка содержат `sourceUrl`. Перегон содержит полную ломаную `coordinates`, а не только две конечные точки. Bbox проверяет каждое ребро ломаной.

В P0 `modes=[auto,history,forecast]`, `stream=false`, `ingestion=false`; годовой горизонт и интервалы неопределённости не объявляются. Доступность прогноза зависит от подготовленного артефакта. Пока данных нет, `/capabilities` возвращает `503 DATA_NOT_READY`; `/health/live` остаётся доступен.

Интерфейс использует `auto` и общий календарь истории и прогноза: с 1 января по 31 декабря 2025 года для текущего набора. Для каждого маршрута и часа фактическое значение, включая ноль, имеет приоритет; при его отсутствии берётся доступный прогноз. Если нет обоих источников, остаётся `null`. Диапазон может пересекать границу истории и прогноза. Явные `history` и `forecast` сохранены для отдельных запросов API.

## Запрос числового просмотра

Один `Scope` используют `POST /map-snapshot`, `/timeseries`, `/route-comparison` и `/heatmap`:

```json
{
  "routeIds": ["1", "5", "17"],
  "metric": "successful_validations",
  "metricScope": "route",
  "mode": "auto",
  "timeRange": {
    "start": "2025-11-01T00:00:00+03:00",
    "end": "2025-11-02T00:00:00+03:00"
  },
  "grain": "hour",
  "aggregation": "sum",
  "snapshotId": "SNAPSHOT_ID_FROM_CAPABILITIES",
  "geometry": {
    "patternIds": [],
    "section": null,
    "bbox": null,
    "referenceMode": "reference"
  }
}
```

Интервал полуоткрытый. Временная зона входа обязательна; после преобразования в `Europe/Moscow` границы должны совпадать с началом часа, а при `grain=day` — с полуночью. Максимум 1464 интервала и 10 уникальных целевых маршрутов. Неизвестные поля запроса отвергаются. Неподдержанный числовой `metricScope` не игнорируется.

`geometry.section` имеет вид `{patternId,fromId,toId}`; обе границы — вхождения одного шаблона в порядке движения. `bbox=[west,south,east,north]`. Геометрический выбор не меняет routeIds, суммы и CSV. Даже при выбранном участке метрика относится ко **всему маршруту**; всегда `geometryFilterAffectsMetric=false`.

`referenceMode=reference` показывает предоставленный поздний срез с предупреждением. `historical` скрывает шаблоны до их `validFrom` / `observedAt` и после известной границы действия. Недоказанная применимость не восстанавливается синтетически. Геометрические ID совпадают с отдельно загруженным `/network`.

## Ответы просмотра

У всех ответов одинаковая `meta`: snapshot/история/сеть, показатель, единицы, шаг, интервал, агрегация, происхождение, описание исторического среднего и ограничения покрытия. В прогнозе заполнены `forecastId`, `modelId`, `issuedAt` и `createdAt`; в истории они `null`. `issuedAt` — модельный origin, `createdAt` — фактическое время создания артефакта.

`meta.provenance` описывает весь запрос: `observation`, `forecast`, `mixed` или `missing`. Каждое маршрутное значение также содержит `provenance`, поэтому карта, таблица и CSV показывают источник своего интервала. В смешанном запросе модельные поля заполнены; в CSV они остаются пустыми у строк с фактическими данными.

| Метод                              | Основное содержимое                                                          |
| ---------------------------------- | ---------------------------------------------------------------------------- |
| `POST /map-snapshot`               | `{meta, frames, geometry}`; frames — весь блок выбранного периода            |
| `POST /timeseries`                 | `{meta, series:[{routeId,points,baseline}]}`                                 |
| `POST /route-comparison`           | `{meta, routes:[{routeId,value,baseline,difference}]}`; сумма за весь запрос |
| `POST /heatmap`                    | `{meta,routeIds,cells:[timeIndex,routeIndex,value][]}`                       |
| `POST /route-profile`              | `422 METRIC_GRAIN_UNAVAILABLE`; остановочного target нет                     |
| `GET /forecast-runs`               | Список сохранённых выпусков, границы обучения, эксперты, модели              |
| `GET /data-quality?snapshotId=...` | Манифест labels, проверки, известные аномалии и ограничения                  |

`Frame`: `{start,end,values,aggregate,baseline}`. В `values` ровно одна запись на каждый выбранный маршрут:

```json
{
  "routeId": "5",
  "provenance": "forecast",
  "value": 0.0,
  "baseline": 0.0,
  "baselineCount": 8,
  "valueOrigins": ["zero_fallback_no_positive_history"],
  "qualityFlags": [],
  "coverageStatus": "forecast",
  "historySupport": "no_positive_history"
}
```

Для истории происхождение — `label` или `filled_zero`; для модели — `model` либо явный zero fallback. Суммарный интервал сохраняет объединение происхождений и флагов. Покрытие `provided_extract` не означает доказанной полноты реального источника.

Отсутствующая величина — `null`, не 0. Если часть периода/маршрутов отсутствует, общий агрегат `null`. За пределами сохранённой истории будущий факт не достраивается. Историческое среднее использует только предшествующие сопоставимые часы; прогнозное среднее зафиксировано на origin. `baselineCount` при суточной агрегации — минимальное число доступных сопоставлений по входящим часам.

`geometry` содержит выбранные ID, географическую версию, дату справочника, скрытые из-за дат шаблоны, список маршрутов без географии и предупреждение. Числовая часть не размножается по числу линий или остановок. Клиент проверяет версии, наборы маршрутов и длины временных массивов до отображения.

## Асинхронный экспорт

`POST /exports`:

```json
{ "scope": "SCOPE_OBJECT", "index": null, "format": "csv" }
```

В реальном запросе `scope` — объект выше. `index` выбирает один frame; `null` означает весь период. Обязателен заголовок `Idempotency-Key` длиной 1–128 символов. Ответ `202`: `{id,kind,status,downloadUrl,error}`. Повтор того же ключа с тем же содержимым возвращает прежнее задание; с другим содержимым — `409 IDEMPOTENCY_CONFLICT`.

`GET /jobs/{id}` или `/exports/{id}`: `pending | running | ready | failed`. При `ready` есть URL `/api/v1/exports/{id}/download`. Пользовательский файл — UTF-8 BOM, `;`, CRLF. Колонки включают маршрут, показатель, значение, интервал, агрегацию, покрытие, происхождение и версии. Числа не округляются; `null` — пустая ячейка. Строки защищены от формул CSV.

`POST /submissions` принимает **только** `{snapshotId}` с тем же заголовком идемпотентности. Фильтры frontend в запросе запрещены. Нужен полный выпуск ноября–декабря из состояния на начало ноября. CSV: UTF-8, `route;date;hour;prediction`, 14 640 уникальных строк, порядок ключей шаблона, округление `floor(value+0.5)`, без индекса. Колонка prediction исходного шаблона не читается.

Переполнение очереди — `429 QUEUE_FULL`, максимум 32 незавершённых задания. Клиент опрашивает задание каждые 0,5 секунды не дольше пяти минут; закрытие окна отменяет ожидание, но уже подтверждённое сервером задание сохраняется.

## Ошибки и эксплуатация

Доменные ошибки: `{error:{code,message}}`. Стандартные ошибки структуры запроса Pydantic возвращают HTTP 422 с `detail`.

Основные коды: `METRIC_UNAVAILABLE`, `METRIC_GRAIN_UNAVAILABLE`, `ROUTE_DATA_UNAVAILABLE`, `VERSION_NOT_FOUND`, `INVALID_SECTION`, `PATTERN_NOT_FOUND`, `RANGE_TOO_LARGE`, `FORECAST_NOT_READY`, `INVALID_COMPETITION_RUN`, `EXPORT_NOT_READY`, `QUEUE_FULL`, `IDEMPOTENCY_CONFLICT`.

`GET /health/live`, `/health/ready`, `/metrics` доступны вне `/api/v1`. Обычный запрос карты не читает исходные CSV, не вызывает погодные API и не запускает обучение. Один контейнер рассчитан на локальную работу без аккаунтов; публичное развёртывание не входит в P0.
