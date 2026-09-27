# Backend

Python 3.12, FastAPI, Pydantic, pandas/PyArrow и CatBoost. Дополнение `models` устанавливает LightGBM, scikit-learn, skops и производственный календарь. Точные версии закреплены в `uv.lock`.

## Локальный запуск

Все команды выполняются из корня репозитория:

```bash
uv sync --directory backend --frozen --extra models
python3 scripts/verify_release.py --assemble deliverables/platform-demo.tar.gz
backend/.venv/bin/moscowt restore deliverables/platform-demo.tar.gz
MOSCOWT_WORKER_ENABLED=false backend/.venv/bin/python -m moscowt.standalone
```

Supervisor запускает API на порту 8000 и оба канала очереди. При старте устанавливаются включённые пассажирский пакет и базовая модель. Чтобы запустить процессы отдельно, используйте `moscowt serve`, `moscowt worker --channel general` и `moscowt worker --channel model`; у API отключите встроенный worker через `MOSCOWT_WORKER_ENABLED=false`.

## Настройки

Переменные имеют префикс `MOSCOWT_` и проверяются классом `Settings`.

| Переменная | Локальное значение по умолчанию |
|---|---|
| `STATE_DIR` | `<проект>/artifacts` |
| `DATASET_DIR` | `<проект>/dataset` |
| `SOURCE_DIR` | Значение `DATASET_DIR` |
| `DATA_ROOT` | Каталог рядом с `STATE_DIR` с суффиксом `-data` |
| `FRONTEND_DIR` | `<проект>/frontend/dist` |
| `DEMO` | `false`; восстановление при пустом состоянии |
| `DEMO_BUNDLE` | `<проект>/deliverables/platform-demo.tar.gz` |
| `PASSENGER_PACKAGE` | `<проект>/deliverables/passengers.tar.gz` |
| `BASELINE_PACKAGE` | `<проект>/deliverables/competition-baseline.zip` |
| `MODEL_THREADS` | `2` |
| `REQUEST_THREADS` | `4` |
| `QUEUE_LIMIT` | `32` |
| `CACHE_ENTRIES` / `CACHE_BYTES` | `64` / `67108864` |
| `MAX_FORECAST_HOURS` | `8784` |

В Docker эти пути заданы внутри `/app` и `/data`; привязки к каталогам хоста нет. Настройки CORS задаются JSON-массивом в `MOSCOWT_CORS_ORIGINS`. По умолчанию разрешён локальный Vite.

## Код

- `api.py`, `platform_api.py`, `passenger_api.py` — HTTP-контракты и маршруты.
- `data/` — нормализация, каталог маршрутов и неизменяемые редакции истории.
- `modeling/` — адаптеры моделей, признаки, проверка переносимых пакетов и обновление базы.
- `tasks.py`, `platform_worker.py`, `job_executor.py` — очередь, исполнение и маршрутизация заданий.
- `entrypoint.py`, `standalone.py` — установка поставки и управление процессами.
- `storage.py` — атомарная запись, хеширование и файловые блокировки.
- `constants/` — календарь, признаки, рецепты, лимиты и справочники.

Сервисы получают настройки и репозитории через конструкторы. Чистые функции преобразуют данные без собственного состояния. Схемы Pydantic задают входные данные; HTTP-обработчики передают вычисления сервисам и очереди.

## Проверки и обслуживание

```bash
backend/.venv/bin/ruff check backend/src backend/tests backend/scripts scripts
backend/.venv/bin/ruff format --check backend/src backend/tests backend/scripts scripts
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 backend/.venv/bin/pytest backend/tests -q
backend/.venv/bin/moscowt --help
```

API публикует актуальный OpenAPI по `/openapi.json`. `moscowt openapi` выводит ту же схему для сохранения во внешние инструменты. JSON-схема не дублируется вручную в репозитории.

Для переноса текущего состояния: `moscowt bundle`. Архив появится в `STATE_DIR/exports`; восстановление: `moscowt restore <архив>`. Импорт моделей через HTTP принимает проверяемые ZIP с нативными весами. Конвертация доверенных joblib/pickle доступна отдельным локальным скриптом `backend/scripts/convert_model.py`.
