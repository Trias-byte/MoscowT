import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';
import { Job, Loose, platform, upload } from '../lib/platform';
import type { PlatformData } from '../lib/usePlatform';
import { dates, Period } from './PlatformFields';
import { dateTimeInput, forecastPeriod, inputTimestamp } from '../lib/modelDates';

export function ModelControls({
  data,
  datasetId,
  routeIds,
  busy,
  act,
  scheduleId,
  reuse,
  onPublished,
  completedUpdate,
}: {
  data: PlatformData;
  datasetId: string;
  routeIds: string[];
  busy: boolean;
  act: (fn: () => Promise<unknown>) => void;
  scheduleId: string;
  reuse: boolean;
  onPublished?: () => void;
  completedUpdate?: { model_id?: string; forecast_id?: string };
}) {
  const [modelId, setModelId] = useState(''),
    [modelType, setModelType] = useState('competition_catboost'),
    [forecastId, setForecastId] = useState('');
  const [training, setTraining] = useState<[string, string]>(['', '']);
  const [forecast, setForecast] = useState<[string, string]>(['', '']);
  const [origin, setOrigin] = useState(''),
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
  const initialized = useRef('');
  useEffect(() => {
    const dataset = data.datasets.versions.find((d) => d.id === datasetId);
    if (!dataset || initialized.current === datasetId) return;
    initialized.current = datasetId;
    const at = dateTimeInput(dataset.forecast_origin || dataset.end);
    setTraining([dateTimeInput(dataset.first_observation || dataset.start), at]);
    setOrigin(at);
    const published = data.forecasts.find(
      (f) =>
        f.id === data.capabilities.current_snapshot.forecastId && f.spec.dataset_id === datasetId,
    );
    const model = data.models.find((m) => m.id === published?.spec.model_id);
    setForecast(
      forecastPeriod(
        at,
        !model || model.spec.model_type === 'competition_catboost' ? '61days' : 'year',
      ),
    );
    setModelId(model?.id || '');
    if (model) {
      setModelType(model.spec.model_type);
      setGroups(model.spec.feature_groups || ['calendar']);
      setWeatherId(model.spec.weather_hourly_id || '');
      setAccidentId(model.spec.accident_links_id || '');
    }
  }, [data, datasetId]);
  useEffect(() => {
    if (completedUpdate?.model_id) setModelId(completedUpdate.model_id);
    if (completedUpdate?.forecast_id) setForecastId(completedUpdate.forecast_id);
  }, [completedUpdate]);

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
            <option value="competition_catboost">Базовая · CatBoost × 5 · 61 день</option>
            <option value="lgb_cb_rf">CatBoost / LightGBM / Random Forest</option>
            <option value="seasonal">Недельный профиль</option>
            <option value="annual_scenario">Годовой сценарий</option>
          </select>
        </label>
        <Period hourly label="Период обучения" value={training} onChange={setTraining} />
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
                  end: training[1].slice(0, 10),
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
                weather_hourly_id: modelType === 'lgb_cb_rf' ? weatherId || null : null,
                accident_links_id: modelType === 'lgb_cb_rf' ? accidentId || null : null,
                external_snapshot_id: modelType === 'lgb_cb_rf' ? externalId || null : null,
              }),
            )
          }
        >
          Обучить модель
        </button>
        <label>
          Обученная модель
          <select
            value={modelId}
            onChange={(e) => {
              setModelId(e.target.value);
              if (
                origin &&
                data.models.find((m) => m.id === e.target.value)?.spec.model_type ===
                  'competition_catboost'
              )
                setForecast(forecastPeriod(origin, '61days'));
            }}
          >
            <option value="">Выберите модель</option>
            {data.models
              .filter((m) => m.spec.dataset_id === datasetId)
              .map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label ||
                    (m.spec.model_type === 'competition_catboost'
                      ? 'Базовая · CatBoost × 5'
                      : m.spec.model_type)}
                  {m.competition_result
                    ? ` · score ${m.competition_result.score.toLocaleString('ru')}`
                    : ''}{' '}
                  · до {m.spec.time_range.end.slice(0, 10)} · {m.id.slice(-8)}
                </option>
              ))}
          </select>
        </label>
        {selected?.competition_result && (
          <p role="note">
            Конкурсный score: <b>{selected.competition_result.score.toLocaleString('ru')}</b> —
            результат отправленного прогноза за ноябрь–декабрь 2025, сообщённый пользователем. После
            переобучения качество проверяется заново.
          </p>
        )}
        {selected?.spec.model_type === 'competition_catboost' && (
          <p>Календарь и история спроса. Выпуск в 00:00 МСК, горизонт до 61 дня.</p>
        )}
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
          <input type="datetime-local" value={origin} onChange={(e) => setOrigin(e.target.value)} />
        </label>
        <div>
          {(['day', 'month', '61days', 'year'] as const).map((h) => (
            <button
              key={h}
              disabled={h === 'year' && selected?.spec.model_type === 'competition_catboost'}
              onClick={() => {
                if (origin) setForecast(forecastPeriod(origin, h));
              }}
            >
              {{ day: 'День', month: 'Месяц', '61days': '61 день', year: 'Год' }[h]}
            </button>
          ))}
        </div>
        <Period hourly label="Период прогноза" value={forecast} onChange={setForecast} />
        {selected?.spec.feature_groups?.includes('weather') && (
          <>
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
            <button
              disabled={busy}
              onClick={() => act(() => platform('/weather-forecasts', Job, {}))}
            >
              Получить текущий погодный выпуск
            </button>
            <p>
              Погода выпуска используется в пределах первых суток и доступных часов, далее —
              климатология. Год рассчитывает выбранная модель. Точность дальнего горизонта смотрите
              в отчёте проверки.
            </p>
          </>
        )}
        <button
          disabled={
            busy || !modelId || !datasetId || selected?.spec.dataset_id !== datasetId || !origin
          }
          onClick={() =>
            act(() =>
              platform('/forecast-runs', Job, {
                model_id: modelId,
                dataset_id: datasetId,
                origin: inputTimestamp(origin),
                time_range: dates(...forecast),
                route_ids: routeIds,
                weather_forecast_id: selected?.spec.feature_groups?.includes('weather')
                  ? releaseId || null
                  : null,
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
