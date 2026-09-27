import { z } from 'zod';
import { Job, downloadUrl, platform } from '../lib/platform';

export const dates = (start: string, end: string) => ({
  start: `${start}T00:00:00+03:00`,
  end: `${end}T00:00:00+03:00`,
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
}: {
  label: string;
  value: [string, string];
  onChange: (value: [string, string]) => void;
}) {
  return (
    <fieldset>
      <legend>{label} · конец не включён</legend>
      {value.map((v, i) => (
        <input
          key={i}
          aria-label={`${label}: ${i ? 'конец' : 'начало'}`}
          type="date"
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
  const names: Record<string, string> = {
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
                onClick={() => act(() => platform(`/uploads/${j.result!.id}/apply`, Job, {}))}
              >
                Применить импорт
              </button>
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
