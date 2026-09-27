import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';
import {
  Dataset,
  Forecast,
  Job,
  Loose,
  Model,
  Schedule,
  downloadUrl,
  platform,
  upload,
} from '../lib/platform';

const dates = (start: string, end: string, startHour = 0, endHour = 0) => ({
  start: `${start}T${String(startHour).padStart(2, '0')}:00:00+03:00`,
  end: `${end}T${String(endHour).padStart(2, '0')}:00:00+03:00`,
});
function Period({
  label,
  value,
  onChange,
}: {
  label: string;
  value: [string, string];
  onChange: (v: [string, string]) => void;
}) {
  return (
    <fieldset>
      <legend>{label} · конец не включён</legend>
      <input
        aria-label={`${label}: начало`}
        type="date"
        value={value[0]}
        onChange={(e) => onChange([e.target.value, value[1]])}
      />
      <input
        aria-label={`${label}: конец`}
        type="date"
        value={value[1]}
        onChange={(e) => onChange([value[0], e.target.value])}
      />
    </fieldset>
  );
}
export function PlatformPanel({
  onPublished,
  onScenario,
}: {
  onPublished?: () => void;
  onScenario?: (id: string) => void;
}) {
  const client = useQueryClient();
  const [open, setOpen] = useState(() => localStorage.getItem('platform-open') === 'true');
  const [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  const [datasetId, setDatasetId] = useState(''),
    [modelId, setModelId] = useState(''),
    [forecastId, setForecastId] = useState('');
  const [routes, setRoutes] = useState('1,5,7,11,12,17,25,26,28,50');
  const [importPeriod, setImportPeriod] = useState<[string, string]>(['2025-09-01', '2025-10-01']);
  const [trainingPeriod, setTrainingPeriod] = useState<[string, string]>([
    '2025-01-01',
    '2025-11-01',
  ]);
  const [forecastPeriod, setForecastPeriod] = useState<[string, string]>([
    '2025-11-01',
    '2026-01-01',
  ]);
  const [exportPeriod, setExportPeriod] = useState<[string, string]>(['2025-11-01', '2025-11-02']);
  const [exportHours, setExportHours] = useState<[number, number]>([0, 0]);
  const [origin, setOrigin] = useState('2025-11-01'),
    [kind, setKind] = useState('labels'),
    [mode, setMode] = useState('append');
  const [weatherForecastId, setWeatherForecastId] = useState('');
  const [complete, setComplete] = useState(false),
    [modelType, setModelType] = useState('lgb_cb_rf');
  const [scheduleId, setScheduleId] = useState(''),
    [scheduleScenario, setScheduleScenario] = useState(false);
  const [externalId, setExternalId] = useState(''),
    [groups, setGroups] = useState(['calendar']);
  const [coefficients, setCoefficients] = useState({ weather: 1, event: 1, season: 1 }),
    [extraHours, setExtraHours] = useState(0),
    [scenarioId, setScenarioId] = useState('');
  const [routeId, setRouteId] = useState(''),
    [autoScenario, setAutoScenario] = useState(false);
  const [grain, setGrain] = useState('hour'),
    [metricScope, setMetricScope] = useState('route'),
    [objectIds, setObjectIds] = useState('');
  const data = useQuery({
    queryKey: ['platform'],
    queryFn: async () => {
      const [datasets, models, forecasts, schedules, external, capabilities, weatherForecasts] =
        await Promise.all([
          platform(
            '/datasets',
            z.object({ current_id: z.string().nullable(), versions: z.array(Dataset) }),
          ),
          platform('/models', z.array(Model)),
          platform('/forecast-runs', z.array(Forecast)),
          platform('/schedules', z.array(Schedule)),
          platform(
            '/external-sources',
            z.object({
              sources: z.array(Loose),
              snapshots: z.array(Loose),
              evaluations: z.array(Loose),
            }),
          ),
          platform(
            '/capabilities',
            z.object({
              current_snapshot: z.object({
                snapshotId: z.string().optional(),
                forecastId: z.string().optional(),
              }),
            }),
          ),
          platform(
            '/weather-forecasts',
            z.array(
              z.object({
                id: z.string(),
                available_at: z.string(),
                start: z.string(),
                end: z.string(),
              }),
            ),
          ),
        ]);
      return { datasets, models, forecasts, schedules, external, capabilities, weatherForecasts };
    },
    enabled: open,
  });
  const jobs = useQuery({
    queryKey: ['platform-jobs'],
    queryFn: () => platform('/jobs', z.array(Job)),
    refetchInterval: 2000,
  });
  const jobSignature = jobs.data?.map((j) => `${j.id}:${j.status}`).join(',');
  useEffect(() => {
    client.invalidateQueries({ queryKey: ['platform'] });
  }, [jobSignature, client]);
  useEffect(() => {
    if (!datasetId && data.data?.datasets.current_id) setDatasetId(data.data.datasets.current_id);
    const publishedId = data.data?.capabilities.current_snapshot.forecastId;
    if (!forecastId && publishedId) {
      setForecastId(publishedId);
      const run = data.data?.forecasts.find((f) => f.id === publishedId);
      if (run && !modelId) setModelId(run.spec.model_id);
    }
  }, [data.data, datasetId, forecastId, modelId]);
  useEffect(() => {
    localStorage.setItem('platform-open', String(open));
  }, [open]);
  const selectedDataset = data.data?.datasets.versions.find((d) => d.id === datasetId);
  const selectedForecast = data.data?.forecasts.find((f) => f.id === forecastId);
  const selectedModel = data.data?.models.find((m) => m.id === modelId);
  const forecastOnMap = forecastId === data.data?.capabilities.current_snapshot.forecastId;
  const routeIds = routes
    .split(',')
    .map((r) => r.trim())
    .filter(Boolean);
  const previewGrain = exportHours.some(Boolean) ? 'hour' : grain === 'hour' ? 'day' : grain;
  const preview = useQuery({
    queryKey: [
      'scenario-preview',
      forecastId,
      routes,
      exportPeriod,
      exportHours,
      grain,
      scheduleId,
      scheduleScenario,
    ],
    enabled: open && !!selectedForecast,
    queryFn: () =>
      platform(
        '/forecasts/query',
        z.object({
          rows: z.array(
            z.object({
              timestamp: z.string(),
              value: z.number().nullable(),
              vehicle_hours: z.number().nullable(),
            }),
          ),
        }),
        {
          dataset_id: selectedForecast!.spec.dataset_id,
          forecast_id: forecastId,
          route_ids: routeIds,
          time_range: dates(...exportPeriod, ...exportHours),
          mode: 'forecast',
          grain: previewGrain,
          schedule_id: scheduleId || null,
          schedule_scenario: scheduleScenario,
        },
      ),
  });
  const baseTotal = preview.data?.rows.every((r) => r.value !== null)
    ? preview.data.rows.reduce((s, r) => s + (r.value || 0), 0)
    : null;
  const baseHours = preview.data?.rows.every((r) => r.vehicle_hours !== null)
    ? preview.data.rows.reduce((s, r) => s + (r.vehicle_hours || 0), 0)
    : null;
  const formatNumber = (value: number | null) =>
    value === null ? 'Нет данных' : value.toLocaleString('ru-RU', { maximumFractionDigits: 1 });
  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await action();
      await client.invalidateQueries({ queryKey: ['platform'] });
      await jobs.refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  const scenarioSpec = () => ({
    forecast_id: forecastId,
    route_ids: routeIds,
    time_range: dates(...exportPeriod, ...exportHours),
    coefficients,
    additional_vehicle_hours: extraHours,
  });
  useEffect(() => {
    if (!autoScenario || !forecastId || !forecastOnMap) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      platform('/scenarios', z.object({ id: z.string() }), scenarioSpec(), controller.signal)
        .then((s) => {
          setScenarioId(s.id);
          onScenario?.(s.id);
        })
        .catch((e) => {
          if (!controller.signal.aborted) setError(String(e));
        });
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // Only the scenario inputs should issue a new immutable scenario.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    autoScenario,
    forecastId,
    forecastOnMap,
    routes,
    exportPeriod,
    exportHours,
    coefficients,
    extraHours,
  ]);
  const multiplier = coefficients.weather * coefficients.event * coefficients.season;
  return (
    <aside className={`platform-panel ${open ? 'expanded' : ''}`} aria-label="Данные и модели">
      <button className="platform-toggle" onClick={() => setOpen(!open)}>
        Данные и модели {open ? '−' : '+'}{' '}
        <small>
          {jobs.data?.filter((j) => ['pending', 'running'].includes(j.status)).length || 0} в работе
        </small>
      </button>
      {open && (
        <div className="platform-body">
          <p>
            Версии данных, обучение, прогнозы и сценарии. Все периоды задаются независимо от карты,
            в московском времени.
          </p>
          {(error || data.error) && (
            <p role="alert" className="platform-error">
              {error || data.error?.message}
            </p>
          )}
          <label>
            Версия данных
            <select value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
              <option value="">Выберите версию</option>
              {data.data?.datasets.versions.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.start.slice(0, 10)} — {d.end.slice(0, 10)} · {d.id.slice(-8)}
                </option>
              ))}
            </select>
          </label>
          {selectedDataset && (
            <p>
              {selectedDataset.known_rows} известных часов из {selectedDataset.rows};{' '}
              {selectedDataset.total.toLocaleString('ru')} валидаций.{' '}
              <button
                disabled={busy}
                onClick={() => act(() => platform(`/datasets/${datasetId}/activate`, Loose, {}))}
              >
                Сделать текущей версией
              </button>
            </p>
          )}
          <label>
            Маршруты через запятую
            <input value={routes} onChange={(e) => setRoutes(e.target.value)} />
          </label>
          <details>
            <summary>Добавить маршрут</summary>
            <input
              aria-label="ID нового маршрута"
              value={routeId}
              onChange={(e) => setRouteId(e.target.value)}
              placeholder="ID маршрута"
            />
            <button
              disabled={busy || !routeId}
              onClick={() =>
                act(() =>
                  platform('/routes', Loose, {
                    route: { id: routeId, number: routeId, name: `Маршрут ${routeId}` },
                  }),
                )
              }
            >
              Зарегистрировать
            </button>
            <p>
              После регистрации импортируйте историю и обучите модель. Трасса загружается через
              существующее окно CSV маршрута.
            </p>
          </details>
          <details open>
            <summary>Импорт истории</summary>
            <Period label="Период импорта" value={importPeriod} onChange={setImportPeriod} />
            <select aria-label="Тип истории" value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="labels">Почасовые labels</option>
              <option value="events">События валидаторов</option>
            </select>
            <select
              aria-label="Режим импорта"
              value={mode}
              onChange={(e) => setMode(e.target.value)}
            >
              <option value="append">Добавить</option>
              <option value="upsert">Исправить переданные часы</option>
              <option value="replace">Заменить период выбранных маршрутов</option>
            </select>
            <label>
              <input
                type="checkbox"
                checked={complete}
                onChange={(e) => setComplete(e.target.checked)}
              />
              Подтверждаю полноту: отсутствующие часы означают ноль
            </label>
            <input
              aria-label="Файл истории"
              type="file"
              accept=".csv"
              disabled={busy}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file)
                  act(async () =>
                    platform('/uploads', Job, {
                      blob_id: await upload(file),
                      spec: {
                        kind,
                        mode,
                        time_range: dates(...importPeriod),
                        route_ids: routeIds,
                        complete,
                      },
                    }),
                  );
              }}
            />
            <p>
              CSV с разделителем «;». Для labels: route;date;hour;boardings. Проверка появится в
              списке заданий; публикация выполняется отдельной кнопкой.
            </p>
          </details>
          <details>
            <summary>Расписание</summary>
            <input
              aria-label="Файл расписания"
              type="file"
              accept=".csv"
              disabled={busy}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file)
                  act(async () => platform('/schedules', Job, { blob_id: await upload(file) }));
              }}
            />
            <select
              aria-label="Версия расписания"
              value={scheduleId}
              onChange={(e) => setScheduleId(e.target.value)}
            >
              <option value="">Без расписания</option>
              {data.data?.schedules.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.valid_from} — {s.valid_to} · {s.method}
                </option>
              ))}
            </select>
            <label>
              <input
                type="checkbox"
                checked={scheduleScenario}
                onChange={(e) => setScheduleScenario(e.target.checked)}
              />
              Сценарно перенести недельное расписание на другой период
            </label>
            <p>
              Остановочные отправления дают оценку одновременных рейсов. Это не подтверждённые
              вагоны и не фактическая наполненность.
            </p>
          </details>
          <details open>
            <summary>Обучение и выпуск</summary>
            <Period label="Период обучения" value={trainingPeriod} onChange={setTrainingPeriod} />
            <select
              aria-label="Адаптер модели"
              value={modelType}
              onChange={(e) => setModelType(e.target.value)}
            >
              <option value="lgb_cb_rf">LGB / CatBoost / RF</option>
              <option value="seasonal">Сезонный baseline</option>
              <option value="annual_scenario">Годовой сценарий</option>
            </select>
            <select
              aria-label="Версия внешних данных"
              value={externalId}
              onChange={(e) => setExternalId(e.target.value)}
            >
              <option value="">Без внешнего архива</option>
              {data.data?.external.snapshots.map((s) => (
                <option key={String(s.id)} value={String(s.id)}>
                  {String(s.id)}
                </option>
              ))}
            </select>
            {(['calendar', 'weather', 'events'] as const).map((g) => (
              <label key={g}>
                <input
                  type="checkbox"
                  checked={groups.includes(g)}
                  onChange={(e) =>
                    setGroups(e.target.checked ? [...groups, g] : groups.filter((v) => v !== g))
                  }
                />
                {
                  {
                    calendar: 'Календарь',
                    weather: 'Климатология',
                    events: 'Известные изменения движения',
                  }[g]
                }
              </label>
            ))}
            <button
              disabled={busy || !datasetId}
              onClick={() =>
                act(() =>
                  platform('/training-runs', Job, {
                    dataset_id: datasetId,
                    model_type: modelType,
                    route_ids: routeIds,
                    time_range: dates(...trainingPeriod),
                    external_snapshot_id: externalId || null,
                    feature_groups: groups,
                  }),
                )
              }
            >
              Обучить модель
            </button>
            <select
              aria-label="Обученная модель"
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
            >
              <option value="">Выберите модель</option>
              {data.data?.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.spec.model_type} · до {m.spec.time_range.end.slice(0, 10)} · {m.id.slice(-8)}
                </option>
              ))}
            </select>
            <label>
              Момент выпуска
              <input type="date" value={origin} onChange={(e) => setOrigin(e.target.value)} />
            </label>
            <Period label="Период прогноза" value={forecastPeriod} onChange={setForecastPeriod} />
            {selectedModel && (
              <details>
                <summary>Паспорт модели и ограничения</summary>
                <pre>{JSON.stringify(selectedModel, null, 2)}</pre>
              </details>
            )}
            <label>
              Погода для суточного выпуска
              <select
                aria-label="Погодный выпуск"
                value={weatherForecastId}
                onChange={(e) => setWeatherForecastId(e.target.value)}
              >
                <option value="">Историческая климатология / без погоды</option>
                {data.data?.weatherForecasts.map((w) => (
                  <option key={w.id} value={w.id}>
                    Получен {w.available_at} · {w.start}—{w.end}
                  </option>
                ))}
              </select>
            </label>
            <button
              disabled={busy}
              onClick={() => act(() => platform('/weather-forecasts', Job, {}))}
            >
              Получить погоду Open-Meteo
            </button>
            <p>
              Выпуск должен быть получен до origin. Для месяца и года используется климатология;
              перенос модели на оперативную погоду ещё не валидирован.
            </p>
            <div>
              {(['day', 'month', 'year'] as const).map((h) => (
                <button
                  key={h}
                  onClick={() => {
                    const end = new Date(`${origin}T00:00:00Z`);
                    if (h === 'day') end.setUTCDate(end.getUTCDate() + 1);
                    if (h === 'month') end.setUTCMonth(end.getUTCMonth() + 1);
                    if (h === 'year') end.setUTCFullYear(end.getUTCFullYear() + 1);
                    setForecastPeriod([origin, end.toISOString().slice(0, 10)]);
                    if (h === 'year') {
                      setModelType('annual_scenario');
                      setModelId(
                        data.data?.models.find(
                          (m) =>
                            m.spec.model_type === 'annual_scenario' &&
                            m.spec.dataset_id === datasetId,
                        )?.id || '',
                      );
                    }
                  }}
                >
                  {{ day: 'День', month: 'Месяц', year: 'Год' }[h]}
                </button>
              ))}
            </div>
            <button
              disabled={busy || !modelId || !datasetId}
              onClick={() =>
                act(() =>
                  platform('/forecast-runs', Job, {
                    model_id: modelId,
                    dataset_id: datasetId,
                    origin: `${origin}T00:00:00+03:00`,
                    time_range: dates(...forecastPeriod),
                    route_ids: routeIds,
                    weather_forecast_id: weatherForecastId || null,
                  }),
                )
              }
            >
              Рассчитать прогноз
            </button>
            <select
              aria-label="Готовый прогноз"
              value={forecastId}
              onChange={(e) => {
                setForecastId(e.target.value);
                setScenarioId('');
              }}
            >
              <option value="">Выберите выпуск</option>
              {data.data?.forecasts.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.spec.time_range.start.slice(0, 10)} — {f.spec.time_range.end.slice(0, 10)} ·{' '}
                  {f.id.slice(-8)}
                </option>
              ))}
            </select>
            {selectedForecast && (
              <p>
                {selectedForecast.quality_note} · {selectedForecast.rows} часов
              </p>
            )}
            <button
              disabled={busy || !forecastId}
              onClick={() =>
                act(async () => {
                  await platform('/publications', Loose, {
                    forecast_id: forecastId,
                    schedule_id: scheduleId || null,
                    schedule_scenario: scheduleScenario,
                  });
                  onPublished?.();
                })
              }
            >
              Показать выпуск на карте
            </button>
          </details>
          <details open>
            <summary>Поправки, бюджет и выгрузка</summary>
            <Period
              label="Период сценария и выгрузки"
              value={exportPeriod}
              onChange={setExportPeriod}
            />
            {(['начало', 'конец'] as const).map((label, i) => (
              <label key={label}>
                Час: {label} · МСК
                <select
                  aria-label={`Час сценария: ${label}`}
                  value={exportHours[i]}
                  onChange={(e) =>
                    setExportHours(
                      i === 0
                        ? [Number(e.target.value), exportHours[1]]
                        : [exportHours[0], Number(e.target.value)],
                    )
                  }
                >
                  {Array.from({ length: 24 }, (_, hour) => (
                    <option key={hour} value={hour}>
                      {String(hour).padStart(2, '0')}:00
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <button
              onClick={() => {
                setExportPeriod([origin, origin]);
                setExportHours([8, 9]);
                setGrain('hour');
              }}
            >
              Выбрать час 08:00–09:00 дня выпуска
            </button>
            {(['weather', 'event', 'season'] as const).map((key) => (
              <label key={key}>
                {{ weather: 'Погода', event: 'Событие', season: 'Сезон' }[key]} ×{' '}
                {coefficients[key].toFixed(2)}
                <input
                  aria-label={`Коэффициент ${key}`}
                  type="range"
                  min="0"
                  max="2"
                  step="0.05"
                  value={coefficients[key]}
                  onChange={(e) =>
                    setCoefficients({ ...coefficients, [key]: Number(e.target.value) })
                  }
                />
              </label>
            ))}
            <p>
              Спрос: {(multiplier * 100).toFixed(1)}% базового, изменение{' '}
              {((multiplier - 1) * 100).toFixed(1)}%. Ручной сценарий не доказывает влияние
              источника.
            </p>
            {preview.error && <p role="alert">{preview.error.message}</p>}
            <table aria-label="Сравнение сценария">
              <thead>
                <tr>
                  <th>Показатель</th>
                  <th>База</th>
                  <th>Сценарий</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Валидации</td>
                  <td>{formatNumber(baseTotal)}</td>
                  <td>{formatNumber(baseTotal === null ? null : baseTotal * multiplier)}</td>
                </tr>
                <tr>
                  <td>Вагоно-часы</td>
                  <td>{formatNumber(baseHours)}</td>
                  <td>{formatNumber(baseHours === null ? null : baseHours + extraHours)}</td>
                </tr>
                <tr>
                  <td>Валидации / вагоно-час</td>
                  <td>
                    {formatNumber(baseTotal !== null && baseHours ? baseTotal / baseHours : null)}
                  </td>
                  <td>
                    {formatNumber(
                      baseTotal !== null && baseHours !== null && baseHours + extraHours > 0
                        ? (baseTotal * multiplier) / (baseHours + extraHours)
                        : null,
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
            <p>
              Предварительный расчёт для выбранного периода; сохранение и выгрузка проверяются
              сервером. Без исходного выпуска оценка добавочных часов недоступна.
            </p>
            <label>
              Дополнительные вагоно-часы на весь период
              <input
                type="number"
                min="0"
                value={extraHours}
                onChange={(e) => setExtraHours(Number(e.target.value))}
              />
            </label>
            <label>
              <input
                type="checkbox"
                checked={autoScenario}
                disabled={!forecastOnMap}
                onChange={(e) => setAutoScenario(e.target.checked)}
              />
              Сразу применять поправки к выбранному выпуску на карте
            </label>
            <button
              disabled={busy || !forecastId}
              onClick={() =>
                act(async () => {
                  const s = await platform(
                    '/scenarios',
                    z.object({ id: z.string() }),
                    scenarioSpec(),
                  );
                  setScenarioId(s.id);
                  if (forecastOnMap) onScenario?.(s.id);
                })
              }
            >
              Сохранить сценарий
            </button>
            <button
              onClick={() => {
                setAutoScenario(false);
                setScenarioId('');
                setCoefficients({ weather: 1, event: 1, season: 1 });
                setExtraHours(0);
                onScenario?.('');
              }}
            >
              Вернуться к базе
            </button>
            {!forecastOnMap && (
              <p>Чтобы применить сценарий к карте, сначала опубликуйте выбранный выпуск.</p>
            )}
            <select
              aria-label="Детализация выгрузки"
              value={metricScope}
              onChange={(e) => setMetricScope(e.target.value)}
            >
              <option value="route">Маршрутные валидации</option>
              <option value="stop">Посадки по остановкам · сценарий</option>
              <option value="segment">Поток через участки · сценарий</option>
            </select>
            {metricScope !== 'route' && (
              <input
                aria-label="ID остановок или участков"
                placeholder="ID через запятую; пусто — все"
                value={objectIds}
                onChange={(e) => setObjectIds(e.target.value)}
              />
            )}
            <select
              aria-label="Шаг выгрузки"
              value={grain}
              onChange={(e) => setGrain(e.target.value)}
            >
              <option value="hour">Часы</option>
              <option value="day">Сутки</option>
              <option value="month">Месяцы</option>
            </select>
            {preview.data && (
              <details>
                <summary>
                  Динамика выбранного выпуска (
                  {previewGrain === 'month'
                    ? 'по месяцам'
                    : previewGrain === 'hour'
                      ? 'по часам'
                      : 'по дням'}
                  )
                </summary>
                <div className="platform-series">
                  <table>
                    <thead>
                      <tr>
                        <th>Дата · МСК</th>
                        <th>Валидации · база</th>
                        <th>Сценарий</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Array.from(new Set(preview.data.rows.map((r) => r.timestamp))).map(
                        (date) => {
                          const rows = preview.data!.rows.filter((r) => r.timestamp === date);
                          const value = rows.every((r) => r.value !== null)
                            ? rows.reduce((sum, r) => sum + (r.value || 0), 0)
                            : null;
                          return (
                            <tr key={date}>
                              <td>
                                {new Date(date).toLocaleDateString('ru-RU', {
                                  timeZone: 'Europe/Moscow',
                                })}
                              </td>
                              <td>{formatNumber(value)}</td>
                              <td>{formatNumber(value === null ? null : value * multiplier)}</td>
                            </tr>
                          );
                        },
                      )}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
            {['csv', 'parquet', 'competition'].map((format) => (
              <button
                key={format}
                disabled={busy || !forecastId}
                onClick={() =>
                  act(() =>
                    format === 'competition'
                      ? platform('/competition-exports', Job, { forecast_id: forecastId })
                      : platform('/exports', Job, {
                          dataset_id: selectedForecast?.spec.dataset_id || datasetId,
                          forecast_id: forecastId,
                          route_ids: routeIds,
                          time_range: dates(...exportPeriod, ...exportHours),
                          mode: 'forecast',
                          grain,
                          format,
                          metric_scope: metricScope,
                          object_ids: objectIds
                            .split(',')
                            .map((v) => v.trim())
                            .filter(Boolean),
                          snapshot_id: data.data?.capabilities.current_snapshot.snapshotId,
                          scenario_id: scenarioId || null,
                          schedule_id: scheduleId || null,
                          schedule_scenario: scheduleScenario,
                        }),
                  )
                }
              >
                {format === 'competition' ? 'Конкурсный CSV' : format.toUpperCase()}
              </button>
            ))}
            <p>
              Конкурсный CSV использует базовый выпуск на исходный период ноября–декабря и исходные
              маршруты, независимо от фильтров и сценария.
            </p>
            <button disabled={busy} onClick={() => act(() => platform('/bundles', Job, {}))}>
              Полный архив артефактов
            </button>
          </details>
          <details>
            <summary>Источники и подтверждённый эффект</summary>
            {data.data?.external.sources.map((s) => (
              <p key={String(s.id)}>
                <a href={String(s.url)} target="_blank" rel="noreferrer">
                  {String(s.id)}
                </a>
                : {String(s.effect_status)} {String(s.limitation || '')}
              </p>
            ))}
            <p>
              Погодный архив — ретроспективный ERA5. Прогноз использует климатологию прошлых полных
              лет. Годовой горизонт и участки являются сценариями.
            </p>
            {data.data?.external.evaluations.map((report, i) => (
              <pre key={i}>{JSON.stringify(report, null, 2)}</pre>
            ))}
          </details>
          <details open>
            <summary>Задания</summary>
            {jobs.data?.map((j) => (
              <article className="platform-job" key={j.id}>
                <b>
                  {j.kind} · {j.status}
                </b>
                {j.error && <p role="alert">{j.error}</p>}
                {j.result && (
                  <details>
                    <summary>Результат проверки</summary>
                    <pre>{JSON.stringify(j.result, null, 2)}</pre>
                  </details>
                )}
                {j.kind === 'import_preview' &&
                  j.status === 'ready' &&
                  typeof j.result?.id === 'string' && (
                    <button
                      disabled={busy}
                      onClick={() => act(() => platform(`/uploads/${j.result!.id}/apply`, Job, {}))}
                    >
                      Опубликовать импорт
                    </button>
                  )}
                {typeof j.result?.download_url === 'string' && (
                  <a href={downloadUrl(j.id)}>Скачать</a>
                )}
                {['pending', 'running'].includes(j.status) && (
                  <button onClick={() => act(() => platform(`/jobs/${j.id}/cancel`, Job, {}))}>
                    Отменить
                  </button>
                )}
              </article>
            ))}
          </details>
        </div>
      )}
    </aside>
  );
}
