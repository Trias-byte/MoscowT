import { useEffect, useRef, useState } from 'react';
import { applyRoute, download, previewRoute, type ImportPreview } from '../lib/api';
import type { Network } from '../lib/contracts';
import { Modal } from './Dialogs';

const columns =
  'route_id;route_name;direction;valid_from;valid_to;point_order;longitude;latitude;stop_id;stop_name';
const cell = (value: string | number) => `"${String(value).replaceAll('"', '""')}"`;

export function routeCsv(network: Network, routeId: string) {
  const route = network.routes.find((r) => r.id === routeId);
  const rows: (string | number)[][] = [];
  for (const pattern of network.patterns.filter((p) => p.routeId === routeId)) {
    let order = 0;
    const segments = network.segments
      .filter((s) => s.patternId === pattern.id)
      .sort((a, b) => a.order - b.order);
    for (const [i, segment] of segments.entries()) {
      const points = i === 0 ? segment.coordinates : segment.coordinates.slice(1);
      for (const [j, point] of points.entries()) {
        const stopId =
          j === points.length - 1 ? segment.toId : i === 0 && j === 0 ? segment.fromId : '';
        const stop = network.stops.find((s) => s.id === stopId);
        rows.push([
          routeId,
          route?.name ?? routeId,
          pattern.direction,
          network.asOf,
          '2026-01-01',
          ++order,
          ...point,
          stop?.stationId ?? '',
          stop?.name ?? '',
        ]);
      }
    }
  }
  return (
    '\ufeff' + columns + '\r\n' + rows.map((row) => row.map(cell).join(';')).join('\r\n') + '\r\n'
  );
}

export function RouteUpload(p: {
  network: Network;
  snapshotId: string;
  routeId: string;
  onClose: () => void;
  onApplied: (result: { snapshotId: string; routeId: string; validFrom: string }) => void;
}) {
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [filename, setFilename] = useState('');
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  const route = p.network.routes.find((r) => r.id === p.routeId);
  async function inspect(file?: File) {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setError('');
    setPreview(null);
    setFilename(file?.name ?? '');
    if (!file) {
      setBusy(false);
      return;
    }
    if (file.size > 5_000_000) {
      setError('Файл должен быть не больше 5 МБ');
      setBusy(false);
      return;
    }
    setBusy(true);
    try {
      const text = await file.text();
      controller.signal.throwIfAborted();
      setPreview(await previewRoute(p.snapshotId, file.name, text, controller.signal));
    } catch (e) {
      if (!controller.signal.aborted) setError((e as Error).message);
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function apply() {
    if (!preview) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError('');
    try {
      p.onApplied(await applyRoute(preview.id, p.snapshotId, controller.signal));
    } catch (e) {
      if (!controller.signal.aborted) setError((e as Error).message);
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <Modal title="Загрузка маршрута из CSV" onClose={p.onClose}>
      <div className="v2-route-upload">
        <p>
          Добавьте новый маршрут или измените направление существующего. Версия появится на карте в
          указанный период 2025 года.
        </p>
        <div className="v2-upload-actions">
          <button
            className="v2-outline"
            onClick={() =>
              download(
                new Blob(['\ufeff' + columns + '\r\n'], { type: 'text/csv;charset=utf-8' }),
                'route-template.csv',
              )
            }
          >
            Скачать шаблон CSV
          </button>
          <button
            className="v2-outline"
            disabled={!route?.hasGeometry}
            onClick={() =>
              download(
                new Blob([routeCsv(p.network, p.routeId)], { type: 'text/csv;charset=utf-8' }),
                `route-${p.routeId}-${p.network.asOf}.csv`,
              )
            }
          >
            Скачать маршрут № {p.routeId} для изменения
          </button>
        </div>
        <details>
          <summary>Формат файла</summary>
          <p>
            UTF-8, разделитель «;» или «,». Один маршрут и один период на файл. Каждая строка —
            точка трассы, включая промежуточные точки между остановками.
          </p>
          <dl>
            <dt>route_id, route_name</dt>
            <dd>Номер и название маршрута.</dd>
            <dt>direction, point_order</dt>
            <dd>Направление 0 или 1; порядок точек от 1 без пропусков.</dd>
            <dt>valid_from, valid_to</dt>
            <dd>
              Даты YYYY-MM-DD. Начало включительно, конец не включительно. Для всего года:
              2025-01-01 и 2026-01-01.
            </dd>
            <dt>longitude, latitude</dt>
            <dd>Долгота и широта WGS84 с десятичной точкой.</dd>
            <dt>stop_id, stop_name</dt>
            <dd>
              Код и название остановки; у промежуточных точек пустые. Первая и последняя точки —
              остановки.
            </dd>
          </dl>
        </details>
        <label className="v2-field">
          CSV маршрута
          <input
            type="file"
            accept=".csv,text/csv"
            disabled={busy}
            onChange={(e) => void inspect(e.target.files?.[0])}
          />
        </label>
        {busy && <p role="status">Обрабатываем маршрут…</p>}
        {error && (
          <p role="alert" className="v2-notice">
            {error}
          </p>
        )}
        {preview && (
          <section className="v2-upload-preview" aria-label="Проверенный маршрут">
            <h3>
              № {preview.route.id} · {preview.route.name}
            </h3>
            <p>{filename}</p>
            <p>
              С {preview.validFrom} до {preview.validTo} (не включая конец).
            </p>
            <p>
              Направления: {preview.directions.join(', ')} · остановок: {preview.stopCount} · точек
              трассы: {preview.pointCount}.
            </p>
            <p>
              {preview.replacesExisting
                ? 'Указанные направления заменят исходную трассу на этот период.'
                : 'Будет добавлен новый маршрут на карту.'}
            </p>
            {!!preview.overlappingImports.length && (
              <p className="v2-notice">
                На пересечении дат новая версия заменит: {preview.overlappingImports.join(', ')}.
              </p>
            )}
            {!preview.route.hasData && <p>Пассажиропоток для нового номера отсутствует.</p>}
            <p className="v2-hint">{preview.warning}</p>
          </section>
        )}
        <button className="v2-primary" disabled={!preview || busy} onClick={() => void apply()}>
          Добавить маршрут на карту
        </button>
      </div>
    </Modal>
  );
}
