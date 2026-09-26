# Backend P0

Python 3.12, FastAPI/Pydantic, NumPy, pandas/PyArrow, openpyxl, CatBoost, SQLite. Зависимости зафиксированы в `uv.lock`; HTTP не читает raw и не обучает модели.

## Запуск и CLI

Из `backend/`:

```bash
uv sync --frozen --python 3.12
uv run moscowt bootstrap
uv run moscowt serve --host 127.0.0.1 --port 8000
```

Отдельные стадии:

```bash
uv run moscowt prepare-labels
uv run moscowt prepare-network
uv run moscowt backtest
uv run moscowt train --origin 2025-11-01 --expert auto
uv run moscowt forecast --model-id MODEL_ID --horizon competition_61d
uv run moscowt submission --output ../artifacts/submission.csv
uv run moscowt openapi > openapi.json
```

`MODEL_ID` берётся из JSON команды `train`. Горизонты CLI: `day` (24 часа), `month` (календарный месяц с первого числа), `competition_61d` (1464 часа). Модель применяется только с тем же origin, для которого обучена. UI «день» и «месяц» выбирает представления одного выпуска, не создавая незаметно новый прогноз.

Общие параметры `--state-dir` и `--dataset-dir` передаются **до** подкоманды. Значения по умолчанию относительно рабочего каталога: `../artifacts`, `../dataset`, frontend `../frontend/dist`.

## Конфигурация

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `MOSCOWT_STATE_DIR` | `../artifacts` | Постоянные артефакты и SQLite |
| `MOSCOWT_DATASET_DIR` | `../dataset` | Входные labels, справочник, шаблон |
| `MOSCOWT_FRONTEND_DIR` | `../frontend/dist` | Собранный frontend |
| `MOSCOWT_WORKER_ENABLED` | `true` | Отдельный процесс экспорта |
| `MOSCOWT_QUEUE_LIMIT` | `32` | Максимум pending + running |
| `MOSCOWT_CACHE_ENTRIES` | `64` | Числовые представления |
| `MOSCOWT_CACHE_BYTES` | `67108864` | Консервативный бюджет кэша Python-объектов |
| `MOSCOWT_LOG_REQUESTS` | `false` | Структурированный журнал каждого HTTP-запроса |
| `MOSCOWT_REQUEST_THREADS` | `4` | Ограничение параллельных обработчиков API (1–32) |
| `MOSCOWT_CORS_ORIGINS` | localhost:5173, 127.0.0.1:5173 | JSON-массив разрешённых origin |

Числовые библиотеки в контейнере ограничены одним потоком; CatBoost — двумя. В памяти API до трёх версий матриц и до 32 неизменяемых JSON-манифестов/справочников. Модели загружаются в CLI, не в API.

## Модули и данные

`domain.py` — строгие запросы, временные интервалы и ошибки; `wire.py` — публичные ответы; `pipelines.py` — labels и справочник; `forecasting.py` — признаки, эксперты и временная проверка; `analytics.py` — маршрутные матрицы и геометрия; `storage.py` — неизменяемые версии; `jobs.py`, `worker.py`, `exports.py` — надёжный экспорт; `api.py` и `cli.py` — входные интерфейсы.

SQLite-схема хранится в `src/moscowt/migrations/001_jobs.sql`. Все подтверждённые задания сохраняются с `synchronous=FULL`, ограниченной очередью и уникальным `(kind, idempotency_key)`. Worker держит отдельную блокировку процесса; после получения блокировки возвращает прерванные `running` в `pending`. Третье прерывание завершает задание ошибкой. API перезапускает упавший worker. Файл публикуется через временный файл, fsync и atomic rename до статуса ready.

В `histories/`, `networks/`, `models/`, `forecasts/`, `snapshots/`, `reports/` лежат неизменяемые артефакты. `current.json` — атомарный указатель. Публикация нового указателя не меняет старые версии. Автоочистка диска не выполняется; оператор сохраняет снимки, на которые ссылаются выпуски и экспорты.

## Методика модели

Ранняя проверка: origin 1 мая и 1 июля 2025 года, по 61 дню. Выбирается минимум `sum(abs_error)/sum(actual)` по двум срезам; при равенстве выигрывает сезонный эксперт. Затем один фиксированный контроль 1 сентября — 1 ноября. Финальное обучение завершается 1 ноября; прогноз — `[2025-11-01, 2026-01-01)`.

Сезонный профиль: среднее полной сетки предыдущих восьми недель по маршруту, дню недели и часу. CatBoost: route/hour/weekday/month как категории, day/weekend/elapsedDay как календарные признаки; MAE, 800 итераций, depth 6, learning rate 0.05, seed 42. Будущие лаги, погода, sample prediction, тарифы, отказы и поздняя география не используются. № 5 получает явный zero fallback.

API сохраняет дробные прогнозы; конкурсный CSV округляет `floor(value + 0.5)`. Порядок ключей берётся из шаблона, который полностью проверяется. WAPE при нулевом знаменателе — `null`, отдельно сохраняется абсолютная ошибка. `issuedAt` — модельный origin ретроспективного выпуска; `createdAt` — фактическое время создания артефакта.
