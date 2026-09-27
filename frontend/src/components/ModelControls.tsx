import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';
import { Job, Loose, platform, upload } from '../lib/platform';
import type { PlatformData } from '../lib/usePlatform';
import { dates, Period } from './PlatformFields';

export function ModelControls({
  data,
  datasetId,
  routeIds,
  busy,
  act,
  scheduleId,
  reuse,
  onPublished,
}: {
  data: PlatformData;
  datasetId: string;
  routeIds: string[];
  busy: boolean;
  act: (fn: () => Promise<unknown>) => void;
  scheduleId: string;
  reuse: boolean;
  onPublished?: () => void;
}) {
  const [modelId, setModelId] = useState(''),
    [modelType, setModelType] = useState('lgb_cb_rf'),
    [forecastId, setForecastId] = useState('');
  const [training, setTraining] = useState<[string, string]>(['2025-01-01', '2025-11-01']);
  const [forecast, setForecast] = useState<[string, string]>(['2025-11-01', '2025-11-02']);
  const [origin, setOrigin] = useState('2025-11-01'),
    [groups, setGroups] = useState(['calendar']);
  const [weatherId, setWeatherId] = useState(''),
    [accidentId, setAccidentId] = useState(''),
    [externalId, setExternalId] = useState(''),
    [releaseId, setReleaseId] = useState('');
  const [weights, setWeights] = useState({ catboost: 70, lightgbm: 10, random_forest: 20 });
  const [weightsJob, setWeightsJob] = useState('');
  const weightResult = useQuery({
    queryKey: ['weight-job', weightsJob],
    enabled: !!weightsJob,
    queryFn: () => platform(`/jobs/${weightsJob}`, Job),
    refetchInterval: (query) =>
      query.state.data && ['ready', 'failed', 'cancelled'].includes(query.state.data.status)
        ? false
        : 2000,
  });
  useEffect(() => {
    if (weightResult.data?.status === 'ready' && typeof weightResult.data.result?.id === 'string') {
      setModelId(weightResult.data.result.id);
      if (typeof weightResult.data.result.forecast_id === 'string')
        setForecastId(weightResult.data.result.forecast_id);
    }
  }, [weightResult.data]);
  const [uploadId, setUploadId] = useState(''),
    [preview, setPreview] = useState<object | null>(null);
  const selected = data.models.find((m) => m.id === modelId);
  const selectedForecast = data.forecasts.find((f) => f.id === forecastId);
  useEffect(() => {
    const published = data.capabilities.current_snapshot.forecastId;
    if (!forecastId && published) {
      setForecastId(published);
      setModelId(data.forecasts.find((f) => f.id === published)?.spec.model_id || '');
    }
    if (!weatherId && data.factors.weather_hourly.length)
      setWeatherId(data.factors.weather_hourly[0].id);
    if (!accidentId && data.factors.accident_links.length)
      setAccidentId(data.factors.accident_links[0].id);
  }, [data, forecastId, weatherId, accidentId]);
  return (
    <>
      <details open>
        <summary>2. Модели и обучение</summary>
        <label>
          Адаптер модели
          <select
            value={modelType}
            onChange={(e) => {
              setModelType(e.target.value);
              if (e.target.value !== 'lgb_cb_rf') setGroups(['calendar']);
            }}
          >
            <option value="lgb_cb_rf">CatBoost / LightGBM / Random Forest</option>
            <option value="seasonal">Недельный профиль</option>
            <option value="annual_scenario">Годовой сценарий</option>
          </select>
        </label>
        <Period label="Период обучения" value={training} onChange={setTraining} />
        <p>
          Набор: {data.datasets.versions.find((d) => d.id === datasetId)?.name || datasetId}.
          Маршруты: {routeIds.join(', ')}.
        </p>
        <details>
          <summary>Внешние признаки</summary>
          <label>
            Почасовая погода
            <select value={weatherId} onChange={(e) => setWeatherId(e.target.value)}>
              <option value="">Выберите архив</option>
              {data.factors.weather_hourly.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.start.slice(0, 10)}—{f.end.slice(0, 10)} · {f.rows} часов
                </option>
              ))}
            </select>
          </label>
          <button
            disabled={busy}
            onClick={() =>
              act(() =>
                platform('/factor-datasets', Job, {
                  kind: 'weather',
                  start: `${Number(training[0].slice(0, 4)) - 1}-01-01`,
                  end: training[1],
                }),
              )
            }
          >
            Получить архив погоды Open-Meteo
          </button>
          <label>
            ДТП рядом с маршрутами
            <select value={accidentId} onChange={(e) => setAccidentId(e.target.value)}>
              <option value="">Выберите архив</option>
              {data.factors.accident_links.map((f) => (
                <option key={f.id} value={f.id}>
                  Москва 2025 · {f.rows} связей · {f.id.slice(-6)}
                </option>
              ))}
            </select>
          </label>
          <button
            disabled={busy}
            onClick={() => act(() => platform('/factor-datasets', Job, { kind: 'accidents' }))}
          >
            Получить ДТП Москвы за 2025 год
          </button>
          <label>
            Архив событий перевозчика
            <select value={externalId} onChange={(e) => setExternalId(e.target.value)}>
              <option value="">Без архива</option>
              {data.external.snapshots.map((f) => (
                <option key={String(f.id)} value={String(f.id)}>
                  {String(f.id)}
                </option>
              ))}
            </select>
          </label>
          {['calendar', 'weather', 'accidents', 'events'].map((g) => (
            <label key={g}>
              <input
                type="checkbox"
                checked={groups.includes(g)}
                disabled={g !== 'calendar' && modelType !== 'lgb_cb_rf'}
                onChange={(e) =>
                  setGroups(e.target.checked ? [...groups, g] : groups.filter((x) => x !== g))
                }
              />
              {
                {
                  calendar: 'Календарь',
                  weather: 'Температура, влажность, осадки',
                  accidents: 'ДТП · ретроспективная зависимость',
                  events: 'Известные изменения движения',
                }[g]
              }
            </label>
          ))}
          <p>
            У будущих ДТП нет известного заранее значения. В оперативном прогнозе незаявленные
            события отсутствуют. Погода месяца и года — сценарий из прошлых лет.
          </p>
        </details>
        <button
          disabled={busy || !datasetId || !routeIds.length}
          onClick={() =>
            act(() =>
              platform('/training-runs', Job, {
                dataset_id: datasetId,
                model_type: modelType,
                route_ids: routeIds,
                time_range: dates(...training),
                feature_groups: groups,
                weather_hourly_id: weatherId || null,
                accident_links_id: accidentId || null,
                external_snapshot_id: externalId || null,
              }),
            )
          }
        >
          Обучить модель
        </button>
        <label>
          Обученная модель
          <select value={modelId} onChange={(e) => setModelId(e.target.value)}>
            <option value="">Выберите модель</option>
            {data.models
              .filter((m) => m.spec.dataset_id === datasetId)
              .map((m) => (
                <option key={m.id} value={m.id}>
                  {m.spec.model_type} · до {m.spec.time_range.end.slice(0, 10)} · {m.id.slice(-8)}
                </option>
              ))}
          </select>
        </label>
        {selected && (
          <details>
            <summary>Паспорт модели и ограничения</summary>
            <p>
              Период {selected.spec.time_range.start.slice(0, 10)}—
              {selected.spec.time_range.end.slice(0, 10)}. Маршруты{' '}
              {selected.spec.route_ids.join(', ')}.
            </p>
            <pre>{JSON.stringify(selected, null, 2)}</pre>
          </details>
        )}
        <details>
          <summary>Загрузить модель / изменить веса ансамбля</summary>
          <p>
            Пакет «Потока» содержит обученную модель и описание её входов. Старые .pkl/.joblib
            сначала преобразуются локальным конвертером.
          </p>
          <input
            aria-label="Пакет модели"
            type="file"
            accept=".zip"
            disabled={busy}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file)
                act(async () => {
                  setUploadId('');
                  setPreview(null);
                  const id = await upload(file, 'model');
                  const report = await platform('/models/import-preview', Loose, { blob_id: id });
                  setUploadId(id);
                  setPreview(report);
                });
            }}
          />
          {preview && <pre>{JSON.stringify(preview, null, 2)}</pre>}
          <button
            disabled={busy || !uploadId}
            onClick={() => act(() => platform('/models/import', Job, { blob_id: uploadId }))}
          >
            Загрузить проверенную модель
          </button>
          <button
            disabled={busy || !modelId}
            onClick={() => act(() => platform(`/models/${modelId}/export`, Job, {}))}
          >
            Подготовить пакет выбранной модели
          </button>
          {(Object.keys(weights) as (keyof typeof weights)[]).map((key) => (
            <label key={key}>
              {key} · %
              <input
                type="number"
                min="0"
                max="100"
                value={weights[key]}
                onChange={(e) => setWeights({ ...weights, [key]: Number(e.target.value) })}
              />
            </label>
          ))}
          <p>
            Сумма: {Object.values(weights).reduce((a, b) => a + b, 0)}%. Изменение весов создаёт
            редакцию без переобучения деревьев.
          </p>
          <button
            disabled={
              busy ||
              selected?.spec.model_type !== 'lgb_cb_rf' ||
              Math.abs(Object.values(weights).reduce((a, b) => a + b, 0) - 100) > 1e-8
            }
            onClick={() =>
              act(async () => {
                const job = await platform(
                  `/models/${modelId}/weights${selectedForecast?.spec.model_id === modelId ? `?forecast_id=${forecastId}` : ''}`,
                  Job,
                  Object.fromEntries(Object.entries(weights).map(([k, v]) => [k, v / 100])),
                );
                setWeightsJob(job.id);
              })
            }
          >
            Сохранить веса новой редакцией
          </button>
          {weightResult.data && (
            <p role="status">
              Редакция весов: {weightResult.data.status}.{' '}
              {weightResult.data.result?.forecast_id
                ? 'Связанный прогноз пересчитан; для карты опубликуйте новый выпуск.'
                : 'Выберите редакцию и рассчитайте нужный период.'}
            </p>
          )}
        </details>
      </details>
      <details open>
        <summary>3. Выпуски прогноза</summary>
        <label>
          Момент выпуска
          <input type="date" value={origin} onChange={(e) => setOrigin(e.target.value)} />
        </label>
        <div>
          {(['day', 'month', 'year'] as const).map((h) => (
            <button
              key={h}
              onClick={() => {
                const end = new Date(`${origin}T00:00:00Z`);
                if (h === 'day') end.setUTCDate(end.getUTCDate() + 1);
                if (h === 'month') end.setUTCMonth(end.getUTCMonth() + 1);
                if (h === 'year') {
                  end.setUTCFullYear(end.getUTCFullYear() + 1);
                  setModelType('annual_scenario');
                  setModelId(
                    data.models.find(
                      (m) =>
                        m.spec.model_type === 'annual_scenario' && m.spec.dataset_id === datasetId,
                    )?.id || '',
                  );
                }
                setForecast([origin, end.toISOString().slice(0, 10)]);
              }}
            >
              {{ day: 'День', month: 'Месяц', year: 'Год' }[h]}
            </button>
          ))}
        </div>
        <Period label="Период прогноза" value={forecast} onChange={setForecast} />
        <label>
          Погодный выпуск для суток
          <select value={releaseId} onChange={(e) => setReleaseId(e.target.value)}>
            <option value="">Климатология / без погоды</option>
            {data.weather.map((w) => (
              <option key={w.id} value={w.id}>
                Получен {w.available_at}
              </option>
            ))}
          </select>
        </label>
        <button disabled={busy} onClick={() => act(() => platform('/weather-forecasts', Job, {}))}>
          Получить текущий погодный выпуск
        </button>
        <p>
          Выпуск погоды должен быть получен до момента прогноза. Годовой результат —
          невалидированный сценарий.
        </p>
        <button
          disabled={busy || !modelId || !datasetId}
          onClick={() =>
            act(() =>
              platform('/forecast-runs', Job, {
                model_id: modelId,
                dataset_id: datasetId,
                origin: `${origin}T00:00:00+03:00`,
                time_range: dates(...forecast),
                route_ids: routeIds,
                weather_forecast_id: releaseId || null,
              }),
            )
          }
        >
          Рассчитать прогноз
        </button>
        <label>
          Готовый прогноз
          <select value={forecastId} onChange={(e) => setForecastId(e.target.value)}>
            <option value="">Выберите выпуск</option>
            {data.forecasts.map((f) => (
              <option key={f.id} value={f.id}>
                {f.spec.time_range.start.slice(0, 10)}—{f.spec.time_range.end.slice(0, 10)} ·{' '}
                {f.id.slice(-8)}
              </option>
            ))}
          </select>
        </label>
        {selectedForecast && (
          <p>
            {selectedForecast.quality_note} · {selectedForecast.rows} маршрутных часов
          </p>
        )}
        <button
          disabled={busy || !forecastId}
          onClick={() =>
            act(async () => {
              await platform('/publications', z.object({ snapshotId: z.string() }), {
                forecast_id: forecastId,
                schedule_id: scheduleId || null,
                schedule_scenario: reuse,
              });
              onPublished?.();
            })
          }
        >
          Показать выпуск на карте
        </button>
      </details>
    </>
  );
}
