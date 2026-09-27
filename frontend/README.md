# Frontend

React 19, TypeScript, Vite, TanStack Query, MapLibre, ECharts и Zod. Версии фиксирует `package-lock.json`.

Из корня проекта:

```bash
npm ci --prefix frontend
npm run dev --prefix frontend
```

Vite обслуживает <http://localhost:5173> и направляет `/api` и `/health` на backend по порту 8000. Запуск backend описан в [его README](../backend/README.md).

## Структура

- `components/` — карта, аналитика, формы моделей, сценариев и пассажиров.
- `lib/` — контракты ответов, React hooks и чистые преобразования данных.
- `services/ApiClient.ts` — HTTP-запросы, загрузки, проверка ответов и сообщения ошибок.
- `constants/` — адреса API, интервалы ожидания, подписи, шкалы и цвета.
- `data/` — стиль карты, опорная геометрия и явно включаемое синтетическое демо.
- `public/assets/` — автономные шрифты, схема путей и Swagger UI с лицензиями.

`VITE_API_URL` по умолчанию равен `/api/v1`. `VITE_DEMO=true` включает синтетическое демо интерфейса; Docker использует реальные включённые данные и `VITE_DEMO=false`. Переменные Vite применяются при сборке.

## Проверки

```bash
npm run typecheck --prefix frontend
npm test --prefix frontend
npm run format:check --prefix frontend
npm run build --prefix frontend
PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 npm run test:e2e --prefix frontend
```

Playwright запускайте на отдельной тестовой поставке: часть проверок импортирует данные, обучает модели и меняет публикацию. При собственном Chromium задайте `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`.
