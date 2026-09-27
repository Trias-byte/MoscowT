import { z } from 'zod';
import type { PlatformData } from '../lib/usePlatform';

const names: Record<string, string> = {
  weather: 'Погода',
  calendar: 'Календарь',
  accidents: 'ДТП',
  events: 'Сообщения перевозчика',
  traffic: 'Загруженность дорог',
  schedule: 'Расписание',
};
const statuses: Record<string, string> = {
  improved: 'Улучшение',
  worse: 'Ухудшение',
  unavailable: 'Нет пригодного ряда',
  unconfirmed: 'Эффект не подтверждён',
  not_evaluated: 'Не проверено',
};
const Effect = z.object({
  modes: z
    .record(
      z.string(),
      z.object({ mean_wape_improvement: z.number(), min: z.number(), max: z.number() }),
    )
    .optional(),
});
export function ExternalEvidence({ external }: { external: PlatformData['external'] }) {
  const latest = external.evaluations.at(-1);
  const effects = z.record(z.string(), Effect).safeParse(latest?.effects);
  const fmt = (n: number) => (100 * n).toFixed(3);
  return (
    <details>
      <summary>Источники и результаты проверки</summary>
      {external.sources.map((s) => (
        <p key={String(s.id)}>
          <a href={String(s.url)} target="_blank" rel="noreferrer">
            {names[String(s.id)] || String(s.id)}
          </a>
          : {statuses[String(s.effect_status)] || String(s.effect_status)}
        </p>
      ))}
      <p>
        Изменение WAPE без фактора минус с фактором, в процентных пунктах. Плюс означает улучшение.
        Одинаковые модели проверены на июне, сентябре и октябре 2025; это проверочные периоды, не
        независимый тест.
      </p>
      {effects.success && (
        <table>
          <thead>
            <tr>
              <th>Фактор</th>
              <th>Оперативно</th>
              <th>Ретроспектива</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(effects.data)
              .filter(([, e]) => e.modes)
              .map(([key, e]) => (
                <tr key={key}>
                  <td>{names[key] || key}</td>
                  {['fixed_origin_climatology', 'retrospective_diagnostic'].map((mode) => (
                    <td key={mode}>
                      {e.modes?.[mode] ? (
                        <>
                          {fmt(e.modes[mode].mean_wape_improvement)}
                          <br />
                          <small>
                            {fmt(e.modes[mode].min)}…{fmt(e.modes[mode].max)}
                          </small>
                        </>
                      ) : (
                        'Нет оценки'
                      )}
                    </td>
                  ))}
                </tr>
              ))}
          </tbody>
        </table>
      )}
      <p>
        Ретроспектива использует фактические условия прогнозируемого окна и не показывает качество
        оперативного прогноза. Чувствительность в сценарии не доказывает причинность.
      </p>
      <details>
        <summary>Полные протоколы WAPE, MAE и смещения</summary>
        {external.evaluations.map((r, i) => (
          <pre key={i}>{JSON.stringify(r, null, 2)}</pre>
        ))}
      </details>
    </details>
  );
}
