import { z } from 'zod';
import { useState } from 'react';
import { Job, downloadUrl, platform } from '../lib/platform';
import { inputTimestamp } from '../lib/modelDates';

export const dates = (start: string, end: string) => ({
  start: inputTimestamp(start),
  end: inputTimestamp(end),
});
export const ids = (value: string) => [
  ...new Set(
    value
      .split(',')
      .map((x) => x.trim())
      .filter(Boolean),
  ),
];
export function Period({
  label,
  value,
  onChange,
  hourly = false,
}: {
  label: string;
  value: [string, string];
  onChange: (value: [string, string]) => void;
  hourly?: boolean;
}) {
  return (
    <fieldset>
      <legend>{label} · конец не включён</legend>
      {value.map((v, i) => (
        <input
          key={i}
          aria-label={`${label}: ${i ? 'конец' : 'начало'}`}
          type={hourly ? 'datetime-local' : 'date'}
          value={v}
          onChange={(e) => onChange(i ? [value[0], e.target.value] : [e.target.value, value[1]])}
        />
      ))}
    </fieldset>
  );
}
export function JobsPanel({
  jobs,
  act,
}: {
  jobs: z.infer<typeof Job>[];
  act: (fn: () => Promise<unknown>) => void;
}) {
  const [autoUpdate, setAutoUpdate] = useState<Record<string, boolean>>({});
  const names: Record<string, string> = {
    model_refresh: 'Обновление модели и прогноза',
    train: 'Обучение',
    forecast: 'Прогноз',
    scenario_run: 'Сценарий',
    import_preview: 'Проверка данных',
    import_apply: 'Импорт',
    model_import: 'Загрузка модели',
    model_reweight: 'Веса ансамбля',
    factor_fetch: 'Внешние данные',
  };
  const status: Record<string, string> = {
    pending: 'В очереди',
    running: 'Выполняется',
    ready: 'Готово',
    failed: 'Ошибка',
    cancelled: 'Отменено',
  };
  return (
    <details>
      <summary>
        Задания ({jobs.filter((j) => ['pending', 'running'].includes(j.status)).length} в работе)
      </summary>
      {jobs.map((j) => (
        <article className="platform-job" key={j.id}>
          <b>
            {names[j.kind] || j.kind} · {status[j.status]}
          </b>
          {j.error && <p role="alert">{j.error}</p>}
          {j.kind === 'model_refresh' && (
            <>
              <p role="status">
                {(
                  {
                    import: 'Применение данных',
                    factors: 'Подготовка признаков',
                    validation: 'Проверка качества',
                    training: 'Обучение ансамбля',
                    forecast: 'Годовой прогноз',
                    publication: 'Публикация',
                    published: 'Прогноз обновлён',
                    quality_blocked: 'Прежний прогноз сохранён',
                    unchanged: 'Данные не изменились',
                  } as Record<string, string>
                )[j.phase || ''] || status[j.status]}{' '}
                · {Math.round(j.progress * 100)}%
              </p>
              {j.update?.dataset_id && <p>Редакция данных: {j.update.dataset_id}</p>}
              {j.update?.model_id && <p>Модель: {j.update.model_id}</p>}
              {j.update?.forecast_id && <p>Выпуск прогноза: {j.update.forecast_id}</p>}
              {j.update?.excluded_routes &&
                Object.entries(j.update.excluded_routes).map(([route, reason]) => (
                  <p key={route}>
                    Маршрут № {route} пока недоступен: {reason}
                  </p>
                ))}
              {j.update?.evaluation_id && (
                <a
                  href={`/api/v2/model-evaluations/${j.update.evaluation_id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Отчёт качества
                </a>
              )}
              {['failed', 'cancelled'].includes(j.status) && (
                <button onClick={() => act(() => platform(`/jobs/${j.id}/retry`, Job, {}))}>
                  Повторить обновление
                </button>
              )}
            </>
          )}
          {j.result && (
            <details>
              <summary>Результат проверки</summary>
              <pre>{JSON.stringify(j.result, null, 2)}</pre>
            </details>
          )}
          {j.kind === 'import_preview' &&
            j.status === 'ready' &&
            typeof j.result?.id === 'string' && (
              <>
                {j.result.update_forecast_default === true && (
                  <label>
                    <input
                      type="checkbox"
                      checked={autoUpdate[j.id] ?? true}
                      onChange={(e) => setAutoUpdate({ ...autoUpdate, [j.id]: e.target.checked })}
                    />
                    После импорта обновить модель и прогноз
                  </label>
                )}
                <button
                  onClick={() =>
                    act(() =>
                      platform(`/uploads/${j.result!.id}/apply`, Job, {
                        update_forecast: autoUpdate[j.id] ?? null,
                      }),
                    )
                  }
                >
                  Применить импорт
                </button>
              </>
            )}
          {j.status === 'ready' && typeof j.result?.filename === 'string' && (
            <a href={downloadUrl(j.id)}>Скачать файл</a>
          )}
          {['pending', 'running'].includes(j.status) && (
            <button onClick={() => act(() => platform(`/jobs/${j.id}/cancel`, Job, {}))}>
              Отменить
            </button>
          )}
        </article>
      ))}
    </details>
  );
}
