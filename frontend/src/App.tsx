import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';
import {
  Activity,
  Download,
  CalendarDays,
  MapPin,
  RefreshCw,
  Route as RouteIcon,
  Search,
  PanelLeftClose,
  PanelRightClose,
  Play,
  Pause,
  ChevronDown,
  ChevronUp,
  TramFront,
} from 'lucide-react';
import type { Capabilities, Scope, ViewState, ObjectSelection, WindowHours } from './lib/contracts';
import {
  getCapabilities,
  getNetwork,
  getGeometry,
  getView,
  exportData,
  serviceDatasetUrl,
  DEMO,
} from './lib/api';
import { initialView, saveView, readUi, saveUi } from './lib/state';
import {
  hourViewAt,
  moscowDateTime,
  makeScope,
  dateBounds,
  intervalLabel,
  dateLabel,
  windowLabel,
} from './lib/time';
import {
  number,
  frameSource,
  sourceLabel,
  sumWindow,
  fleetSourceLabel,
  VEHICLE_LOAD_THRESHOLDS,
} from './lib/domain';
import { NetworkMap } from './components/NetworkMap';
import { Analytics, type ChartTab } from './components/Analytics';
import { Modal } from './components/Dialogs';
import { RouteUpload } from './components/RouteUpload';
import { RouteSections, SectionDetails } from './components/SectionDetails';
import { buildSectionModel, estimateSections } from './lib/sectionLoad';
import { getSections, platform } from './lib/platform';
import { PlatformPanel } from './components/PlatformPanel';
import { ScenarioPanel } from './components/ScenarioPanel';
import { ExportPanel } from './components/ExportPanel';
import { PassengerComposition } from './components/Passengers';
import {
  InfrastructureControls,
  InfrastructureDetails,
  PoiCard,
} from './components/PassengerInfrastructure';
import { parameters, PoiCollection, type PoiFeature } from './lib/passengers';
import { scenarioModes, useScenarioWorkspace, visibleIncidents } from './lib/scenario';
interface Draft {
  scope: Scope;
  timeRange: Scope['timeRange'];
}
export default function App() {
  const capabilities = useQuery({
    queryKey: ['capabilities'],
    queryFn: ({ signal }) => getCapabilities(signal),
    refetchInterval: DEMO ? false : 5000,
  });
  if (capabilities.isError)
    return (
      <main className="v2-start">
        <TramFront size={40} />
        <h1>Не удалось подключиться к backend</h1>
        {!DEMO && <PlatformPanel onPublished={() => capabilities.refetch()} />}
        <p>{capabilities.error.message}</p>
        <p>Запустите подготовку данных и сервер по инструкции README.</p>
        <button className="v2-primary" onClick={() => capabilities.refetch()}>
          Повторить
        </button>
      </main>
    );
  if (!capabilities.data)
    return (
      <main className="v2-start">
        <Activity size={36} />
        <h1>Подключаем данные маршрутов…</h1>
      </main>
    );
  return <Workspace capabilities={capabilities.data} refresh={() => capabilities.refetch()} />;
}
function Workspace({
  capabilities: c,
  refresh,
}: {
  capabilities: Capabilities;
  refresh: () => Promise<unknown>;
}) {
  const [view, setView] = useState<ViewState>(() => initialView(c));
  useEffect(() => {
    setView((previous) => {
      if (
        previous.scenarioId ||
        (previous.publishedSnapshotId ?? previous.snapshotId) === c.snapshotId
      )
        return previous;
      const bounds = dateBounds(c);
      return {
        ...previous,
        snapshotId: c.snapshotId,
        publishedSnapshotId: c.snapshotId,
        forecastId: c.forecastId || undefined,
        scenarioRange: undefined,
        date:
          previous.date >= bounds.min && previous.date <= bounds.max
            ? previous.date
            : c.defaultDate,
        routeIds: previous.routeIds.filter((id) => c.targetRouteIds.includes(id)),
        geometry: { ...previous.geometry, patternIds: [], section: null },
      };
    });
  }, [c.snapshotId]);
  const [selected, setSelected] = useState<ObjectSelection>({ kind: 'route', id: '1' });
  const [left, setLeft] = useState(() => readUi('v2-left', window.innerWidth >= 900)),
    [right, setRight] = useState(() => readUi('v2-right', window.innerWidth > 900)),
    [analyticsOpen, setAnalyticsOpen] = useState(() => readUi('v2-analytics', true)),
    [tab, setTab] = useState<ChartTab>('dynamics');
  const [dateOpen, setDateOpen] = useState(false),
    [uploadOpen, setUploadOpen] = useState(false),
    [draft, setDraft] = useState<Draft | null>(null),
    [help, setHelp] = useState(false),
    [search, setSearch] = useState(''),
    [playing, setPlaying] = useState(false),
    [speed, setSpeed] = useState(1);
  const [showReference, setShowReference] = useState(false),
    [showStops, setShowStops] = useState(true),
    [viewportOnly, setViewportOnly] = useState(false),
    [bounds, setBounds] = useState<[number, number, number, number] | null>(null);
  const [showInfrastructure, setShowInfrastructure] = useState(false);
  const [infrastructureRadius, setInfrastructureRadius] = useState<500 | 1000>(500);
  const [poiCategory, setPoiCategory] = useState('');
  const [selectedPoi, setSelectedPoi] = useState<PoiFeature | null>(null);
  const [from, setFrom] = useState(''),
    [to, setTo] = useState(''),
    [toast, setToast] = useState('');
  const [toolsPanel, setToolsPanel] = useState(() => localStorage.getItem('workspace-tools') || '');
  const scenarioWorkspace = useScenarioWorkspace();
  const { draft: scenarioDraft, mode: scenarioMode } = scenarioWorkspace.record;
  const [hideDraftIncidents, setHideDraftIncidents] = useState(
    () => sessionStorage.getItem('potok-hide-incidents') === 'true',
  );
  const setScenarioDraft = scenarioWorkspace.setDraft;
  const visibleHours = view.scenarioRange
    ? (Date.parse(view.scenarioRange.end) - Date.parse(view.scenarioRange.start)) / 3600000
    : view.windowHours;
  const periodLabel = view.scenarioRange
    ? `${view.scenarioId ? 'период сценария' : 'выбранный период'} (${visibleHours} ч)`
    : windowLabel(view.windowHours);
  const [placingIncident, setPlacingIncident] = useState(false);
  const [selectedIncident, setSelectedIncident] = useState<string | null>(null);
  useEffect(() => {
    localStorage.setItem('workspace-tools', toolsPanel);
  }, [toolsPanel]);
  const previousGeometryVersion = useRef<string | null>(null);
  const detailsPanel = useRef<HTMLElement>(null);
  useEffect(() => {
    detailsPanel.current?.scrollTo({ top: 0 });
  }, [selected]);
  const scope = useMemo(
    () =>
      makeScope({ ...view, geometry: { ...view.geometry, bbox: viewportOnly ? bounds : null } }),
    [view, viewportOnly, bounds],
  );
  const mapDate = view.date;
  const poiParams = parameters({
    snapshot_id: view.snapshotId,
    date: mapDate,
    route_ids: scope.routeIds,
    categories: poiCategory ? [poiCategory] : [],
    radius: infrastructureRadius,
    bbox: bounds,
  });
  const poi = useQuery({
    queryKey: ['passengers', 'poi', poiParams],
    queryFn: ({ signal }) =>
      platform(`/passengers/poi?${poiParams}`, PoiCollection, undefined, signal),
    enabled: showInfrastructure && !DEMO,
    staleTime: 60000,
  });
  useEffect(() => setSelectedPoi(null), [poiParams, showInfrastructure]);
  // The day total still offers an active hourly cursor; moving it selects an hour.
  const lastHour = view.windowHours === 12 ? 12 : 23;
  const numericScope: Scope = {
    ...scope,
    geometry: { patternIds: [], section: null, bbox: null, referenceMode: 'reference' },
  };
  const geometryScope = { ...scope, geometry: { ...scope.geometry, date: mapDate } };
  const network = useQuery({
    queryKey: ['network', view.snapshotId, mapDate],
    queryFn: ({ signal }) => getNetwork(view.snapshotId, signal, mapDate),
    staleTime: Infinity,
  });
  const geometry = useQuery({
    queryKey: ['geometry', geometryScope],
    queryFn: ({ signal }) => getGeometry(geometryScope, signal),
    enabled:
      !!network.data &&
      scope.routeIds.length > 0 &&
      scope.geometry.patternIds.every((id) => network.data!.patterns.some((p) => p.id === id)),
  });
  const data = useQuery({
    queryKey: ['view', numericScope],
    queryFn: ({ signal }) => getView(numericScope, signal),
    enabled: scope.routeIds.length > 0,
  });
  const index = view.scenarioRange ? 0 : Math.min(view.index, lastHour);
  const frame = useMemo(
    () => sumWindow(data.data?.frames ?? [], index, visibleHours),
    [data.data, index, visibleHours],
  );
  const networkData = network.data;
  const sectionScope: Scope = {
    ...geometryScope,
    timeRange: {
      start: new Date(Date.parse(scope.timeRange.start) + index * 3600000).toISOString(),
      end: new Date(
        Date.parse(scope.timeRange.start) + (index + visibleHours) * 3600000,
      ).toISOString(),
    },
  };
  const sections = useQuery({
    queryKey: ['sections', sectionScope],
    queryFn: ({ signal }) => getSections(sectionScope, signal),
    enabled: !DEMO && !!networkData && scope.routeIds.length > 0,
  });
  const sectionLoads = useMemo(
    () =>
      DEMO && networkData
        ? estimateSections(
            buildSectionModel(networkData),
            (data.data?.frames ?? []).slice(index, index + visibleHours),
          )
        : (sections.data ?? {}),
    [networkData, data.data, index, visibleHours, sections.data],
  );
  const selectedSegment =
    selected.kind === 'segment'
      ? networkData?.segments.find((s) => s.id === selected.id)
      : undefined;
  const sectionLoad = selectedSegment ? sectionLoads[selectedSegment.id] : undefined;
  const thresholds = c.vehicleLoadThresholds ?? VEHICLE_LOAD_THRESHOLDS;
  const selectedStop =
    selected.kind === 'stop' ? networkData?.stops.find((s) => s.id === selected.id) : undefined;
  const focused =
    selectedStop?.routeId ??
    (selected.kind === 'segment'
      ? selected.routeId
      : selected.kind === 'route'
        ? selected.id
        : '1');
  const route = networkData?.routes.find((r) => r.id === focused),
    value = frame?.values.find((v) => v.routeId === focused);
  const patterns = networkData?.patterns.filter((p) => p.routeId === focused) ?? [];
  const selectedPattern =
    view.geometry.patternIds.find((id) => patterns.some((p) => p.id === id)) ?? '';
  const occurrences = networkData?.stops.filter((s) => s.patternId === selectedPattern) ?? [];
  const notify = useCallback((text: string) => setToast(text), []);
  const patch = useCallback((update: Partial<ViewState>) => {
    setPlaying(false);
    setView((v) => ({
      ...v,
      ...update,
      ...(!('scenarioRange' in update) &&
      (update.date !== undefined ||
        update.index !== undefined ||
        update.windowHours !== undefined ||
        ('scenarioId' in update && !update.scenarioId))
        ? { scenarioRange: undefined }
        : {}),
      index: Math.min(
        24 - (update.windowHours ?? v.windowHours),
        Math.max(0, update.index ?? v.index),
      ),
    }));
  }, []);
  useEffect(() => saveView(view), [view]);
  useEffect(() => {
    if (
      !view.scenarioId &&
      c.fleetId &&
      data.data &&
      !data.data.meta.fleetId &&
      view.snapshotId !== c.snapshotId
    )
      patch({
        snapshotId: c.snapshotId,
        publishedSnapshotId: c.snapshotId,
        forecastId: c.forecastId || undefined,
        scenarioId: undefined,
      });
  }, [c.fleetId, c.snapshotId, data.data, view.snapshotId, patch]);
  useEffect(() => saveUi('v2-left', left), [left]);
  useEffect(() => saveUi('v2-right', right), [right]);
  useEffect(() => saveUi('v2-analytics', analyticsOpen), [analyticsOpen]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 5000);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    if (!playing || !data.data || view.windowHours === 24) return;
    const timer = setInterval(
      () =>
        setView((v) => {
          if (v.index >= lastHour) {
            setPlaying(false);
            return v;
          }
          return { ...v, index: v.index + 1 };
        }),
      1200 / speed,
    );
    return () => clearInterval(timer);
  }, [playing, speed, data.data, lastHour, view.windowHours]);
  useEffect(() => {
    if (!networkData) return;
    const version = `${networkData.networkSnapshotId}:${networkData.sourceAsOf}:${networkData.patterns.map((p) => p.id).join(',')}`;
    if (previousGeometryVersion.current && previousGeometryVersion.current !== version) {
      setView((v) => ({ ...v, geometry: { ...v.geometry, section: null } }));
      setFrom('');
      setTo('');
      setSelected((s) => (s.kind !== 'route' ? { kind: 'route', id: focused } : s));
    }
    previousGeometryVersion.current = version;
    if (!networkData.historyRange && c.networkSnapshotId !== networkData.networkSnapshotId) {
      patch({
        snapshotId: c.snapshotId,
        geometry: { ...view.geometry, patternIds: [], section: null },
      });
      return;
    }
    const ids = new Set(networkData.patterns.map((p) => p.id));
    if (view.geometry.patternIds.some((id) => !ids.has(id)))
      patch({ geometry: { ...view.geometry, patternIds: [], section: null } });
  }, [networkData, c.snapshotId, c.networkSnapshotId]);
  const select = useCallback((s: ObjectSelection) => {
    setSelected(s);
    setRight(true);
    if (window.innerWidth < 900) setLeft(false);
  }, []);
  function toggleRoute(id: string) {
    patch({
      routeIds: view.routeIds.includes(id)
        ? view.routeIds.filter((r) => r !== id)
        : [...view.routeIds, id],
      geometry: { ...view.geometry, section: null },
    });
  }
  const searchResults = search.trim()
    ? networkData?.stops
        .filter((s) => s.name.toLocaleLowerCase('ru').includes(search.toLocaleLowerCase('ru')))
        .slice(0, 8)
    : [];
  return (
    <main
      className={`workspace-v2 ${toolsPanel ? 'tools-open' : ''} ${placingIncident ? 'placing-incident' : ''}`}
    >
      {!DEMO && (
        <nav className="workspace-tools" aria-label="Работа с данными">
          <button
            aria-pressed={toolsPanel === 'data'}
            onClick={() => setToolsPanel(toolsPanel === 'data' ? '' : 'data')}
          >
            Данные и модели
          </button>
          <button
            aria-pressed={toolsPanel === 'scenarios'}
            onClick={() => setToolsPanel(toolsPanel === 'scenarios' ? '' : 'scenarios')}
          >
            Сценарии
          </button>
        </nav>
      )}
      {!DEMO && (
        <PlatformPanel
          open={toolsPanel === 'data'}
          onClose={() => setToolsPanel('')}
          onUploadRoute={() => setUploadOpen(true)}
          onHelp={() => setHelp(true)}
          onPublished={() => {
            refresh();
            getCapabilities().then((caps) =>
              patch({
                snapshotId: caps.snapshotId,
                publishedSnapshotId: caps.snapshotId,
                forecastId: caps.forecastId || undefined,
                date: caps.defaultDate,
                routeIds: caps.targetRouteIds,
                scenarioId: undefined,
                geometry: { patternIds: [], section: null, bbox: null, referenceMode: 'reference' },
              }),
            );
          }}
        />
      )}

      {!DEMO && (
        <ScenarioPanel
          selectedObject={selected}
          selectedRouteId={focused}
          activeForecastId={view.forecastId ?? c.forecastId}
          open={toolsPanel === 'scenarios'}
          workspace={scenarioWorkspace}
          placing={placingIncident}
          setPlacing={(value) => {
            setPlacingIncident(value);
            if (value) {
              setAnalyticsOpen(false);
              setHideDraftIncidents(false);
              sessionStorage.setItem('potok-hide-incidents', 'false');
            }
          }}
          snapshotId={view.snapshotId}
          selectedIncident={selectedIncident}
          selectIncident={setSelectedIncident}
          onApply={(id, spec, snapshotId) => {
            setHideDraftIncidents(true);
            sessionStorage.setItem('potok-hide-incidents', 'true');
            patch({
              scenarioId: id || undefined,
              scenarioRange: spec?.time_range || view.scenarioRange,
              scenarioName: spec ? `${spec.name} · ${scenarioModes[spec.mode]}` : undefined,
              scenarioIncidents: spec?.incidents,
              ...(id && spec
                ? {
                    snapshotId,
                    forecastId: spec.forecast_id,
                    routeIds: spec.route_ids,
                    date: spec.time_range.start.slice(0, 10),
                    index: 0,
                  }
                : {}),
            });
          }}
          onClose={() => {
            setToolsPanel('');
            setPlacingIncident(false);
          }}
        />
      )}

      <header className="v2-header">
        <a className="v2-brand" href="/" aria-label="Поток — главная">
          <span>
            <TramFront size={22} />
          </span>
          <b>
            поток<span>МОСКОВСКИЙ ТРАМВАЙ</span>
          </b>
        </a>
        <div className="v2-title">
          <h1>Пассажиропоток</h1>
          <span>Успешные валидации · сумма за {periodLabel}</span>
        </div>
        <div className="v2-header-actions">
          <span className="v2-status">
            <i />
            {view.scenarioId
              ? `Сценарий: ${view.scenarioName || 'рассчитанный результат'}`
              : DEMO
                ? 'Демонстрация'
                : 'Данные и прогноз'}
          </span>
          <button
            className="v2-outline"
            disabled={!frame}
            onClick={() =>
              setDraft({
                scope: structuredClone(scope),
                timeRange: { start: frame!.start, end: frame!.end },
              })
            }
          >
            <Download size={16} />
            Экспорт
          </button>
        </div>
      </header>
      {DEMO && (
        <div className="v2-demo-banner">
          Демонстрационный режим: значения синтетические, конкурсный экспорт отключён.
        </div>
      )}
      <div
        className={`v2-workarea ${left ? 'left-open' : ''} ${right && !toolsPanel ? 'right-open' : ''}`}
      >
        {left && (
          <aside className="v2-sidebar" aria-label="Управление маршрутами">
            <div className="v2-panel-heading">
              <h2>Трамвайная сеть</h2>
              <button aria-label="Скрыть управление" onClick={() => setLeft(false)}>
                <PanelLeftClose size={17} />
              </button>
            </div>
            <div className="v2-search">
              <Search size={15} />
              <input
                aria-label="Поиск остановок"
                placeholder="Найти остановку"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            {!!searchResults?.length && (
              <div className="v2-search-results">
                {searchResults.map((s) => (
                  <button
                    key={s.id}
                    onClick={() => {
                      select({ kind: 'stop', id: s.id });
                      setSearch('');
                    }}
                  >
                    {s.name} · № {s.routeId}
                  </button>
                ))}
              </div>
            )}
            <div className="v2-section-title">
              <span>Целевые маршруты</span>
              <b>
                {view.routeIds.length} / {c.targetRouteIds.length}
              </b>
            </div>
            <div className="v2-small-actions">
              <button onClick={() => patch({ routeIds: c.targetRouteIds })}>Все</button>
              <button onClick={() => patch({ routeIds: [] })}>Ни одного</button>
              <button
                onClick={() => patch({ routeIds: route?.isTarget ? [focused] : view.routeIds })}
              >
                Только выбранный
              </button>
            </div>
            <div className="v2-routes">
              {networkData?.routes
                .filter((r) => r.isTarget)
                .map((r) => (
                  <div key={r.id} className={`v2-route ${focused === r.id ? 'selected' : ''}`}>
                    <input
                      type="checkbox"
                      aria-label={`Показать маршрут ${r.id}`}
                      checked={view.routeIds.includes(r.id)}
                      onChange={() => toggleRoute(r.id)}
                    />
                    <button
                      aria-label={`Выбрать маршрут ${r.id}`}
                      onClick={() => {
                        select({ kind: 'route', id: r.id });
                        setFrom('');
                        setTo('');
                      }}
                    >
                      <b>{r.number}</b>
                      <span>
                        {r.name}
                        <small>
                          {(r.sourceKind === 'reconstruction'
                            ? 'Трасса восстановлена по архиву'
                            : r.geometryNote) ||
                            (r.id === '5'
                              ? 'Нет положительной истории'
                              : r.hasGeometry
                                ? 'Пути и остановки на карте'
                                : 'Без географии · данные доступны')}
                        </small>
                      </span>
                    </button>
                  </div>
                ))}
            </div>
            {networkData?.routes.some((r) => !r.isTarget && r.sourceKind === 'user_csv') && (
              <div className="v2-custom-routes">
                <div className="v2-section-title">Загруженные маршруты</div>
                {networkData.routes
                  .filter((r) => !r.isTarget && r.sourceKind === 'user_csv')
                  .map((r) => (
                    <button
                      key={r.id}
                      className="v2-outline full"
                      onClick={() => {
                        setShowReference(true);
                        select({ kind: 'route', id: r.id });
                      }}
                    >
                      № {r.number} · {r.name}
                    </button>
                  ))}
                <p className="v2-hint">Трассы из CSV. Числовые данные отсутствуют.</p>
              </div>
            )}
            <div className="v2-section-title">Географическое представление</div>
            <label className="v2-field">
              Направление маршрута № {focused}
              <select
                value={selectedPattern}
                disabled={!patterns.length || !route?.isTarget}
                onChange={(e) => {
                  setSelected({ kind: 'route', id: focused });
                  patch({
                    geometry: {
                      ...view.geometry,
                      patternIds: e.target.value ? [e.target.value] : [],
                      section: null,
                    },
                  });
                  setFrom('');
                  setTo('');
                }}
              >
                <option value="">Все направления</option>
                {patterns.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <div className="v2-fields-pair">
              <label className="v2-field">
                Начальная остановка
                <select
                  aria-label="Начальная остановка"
                  value={from}
                  disabled={!selectedPattern}
                  onChange={(e) => setFrom(e.target.value)}
                >
                  <option value="">Выберите</option>
                  {occurrences.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.order}. {s.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="v2-field">
                Конечная остановка
                <select
                  aria-label="Конечная остановка"
                  value={to}
                  disabled={!selectedPattern}
                  onChange={(e) => setTo(e.target.value)}
                >
                  <option value="">Выберите</option>
                  {occurrences.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.order}. {s.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <button
              className="v2-outline full"
              disabled={
                !from ||
                !to ||
                (occurrences.find((s) => s.id === from)?.order ?? 0) >=
                  (occurrences.find((s) => s.id === to)?.order ?? 0)
              }
              onClick={() => {
                patch({
                  geometry: {
                    ...view.geometry,
                    section: { patternId: selectedPattern, fromId: from, toId: to },
                  },
                });
                notify('Участок изменяет только карту. Числа относятся ко всему маршруту.');
              }}
            >
              Показать только участок
            </button>
            {view.geometry.section && (
              <button
                className="v2-text"
                onClick={() => patch({ geometry: { ...view.geometry, section: null } })}
              >
                Сбросить участок
              </button>
            )}
            <p className="v2-hint">
              Направление и участок меняют карту. Значения и экспорт остаются маршрутными.
            </p>
            <p className="v2-hint">
              Маршруты на {mapDate}. Версия меняется вместе с датой в календаре.
            </p>
            <label className="v2-check">
              <input
                type="checkbox"
                checked={showStops}
                onChange={(e) => setShowStops(e.target.checked)}
              />
              Остановки на карте
            </label>
            <label className="v2-check">
              <input
                type="checkbox"
                checked={showReference}
                onChange={(e) => setShowReference(e.target.checked)}
              />
              Справочные маршруты без target
            </label>
            <label className="v2-check">
              <input
                type="checkbox"
                checked={viewportOnly}
                onChange={(e) => setViewportOnly(e.target.checked)}
              />
              Геометрия в видимой области
            </label>
            {!DEMO && (
              <InfrastructureControls
                enabled={showInfrastructure}
                onEnabled={setShowInfrastructure}
                radius={infrastructureRadius}
                onRadius={setInfrastructureRadius}
                category={poiCategory}
                onCategory={setPoiCategory}
                data={poi.data}
                error={poi.error}
                loading={poi.isFetching}
                onSelect={setSelectedPoi}
              />
            )}
          </aside>
        )}
        <section className="v2-center">
          <div className="v2-map-toolbar">
            <button
              aria-expanded={left}
              onClick={() => {
                setLeft(!left);
                if (window.innerWidth < 900) setRight(false);
              }}
            >
              <RouteIcon size={16} />
              Маршруты
            </button>
            <span className="v2-status" aria-label="Источник данных">
              {sourceLabel(frameSource(frame))}
            </span>
            <button
              aria-expanded={right}
              onClick={() => {
                setRight(!right);
                if (window.innerWidth < 900) setLeft(false);
              }}
            >
              <MapPin size={16} />
              Объект
            </button>
          </div>
          <div className="v2-map-container">
            {networkData ? (
              <NetworkMap
                poi={showInfrastructure ? poi.data : undefined}
                onPoiSelect={(id) =>
                  setSelectedPoi(poi.data?.features.find((f) => f.id === id) ?? null)
                }
                network={networkData}
                sectionLoads={sectionLoads}
                geometry={geometry.data}
                selected={selected}
                onSelect={select}
                onBounds={setBounds}
                showReference={showReference}
                showStops={showStops}
                loadThresholds={thresholds}
                incidents={visibleIncidents(
                  !hideDraftIncidents &&
                    toolsPanel === 'scenarios' &&
                    ['combined', 'incidents'].includes(scenarioMode) &&
                    scenarioDraft.enabled.incidents
                    ? scenarioDraft.incidents
                    : view.scenarioId
                      ? view.scenarioIncidents || []
                      : [],
                  sectionScope.timeRange,
                )}
                placingIncident={placingIncident}
                onIncidentSelect={(id) => {
                  setSelectedIncident(id);
                  setToolsPanel('scenarios');
                }}
                onIncidentMove={(id, longitude, latitude) =>
                  setScenarioDraft((previous) => ({
                    ...previous,
                    incidents: previous.incidents.map((i) =>
                      i.id === id ? { ...i, longitude, latitude } : i,
                    ),
                  }))
                }
                onIncidentPlace={(longitude, latitude) => {
                  const id = crypto.randomUUID();
                  setScenarioDraft((previous) => ({
                    ...previous,
                    incidents: [
                      ...previous.incidents,
                      {
                        id,
                        longitude,
                        latitude,
                        route_ids: [],
                        start: moscowDateTime(sectionScope.timeRange.start),
                        duration_minutes: 60,
                        reduction: 0.5,
                      },
                    ],
                  }));
                  setSelectedIncident(id);
                  setPlacingIncident(false);
                  setToolsPanel('scenarios');
                }}
              />
            ) : (
              <div className="v2-empty">Загружаем справочную сеть…</div>
            )}
            {showInfrastructure && selectedPoi && (
              <PoiCard feature={selectedPoi} onClose={() => setSelectedPoi(null)} />
            )}
            <div className="v2-total">
              <span>{sourceLabel(frameSource(frame))}</span>
              <strong>{number(frame?.aggregate)}</strong>
              <small>
                По {view.routeIds.length} маршрутам за {periodLabel}
              </small>
            </div>
            {(network.isError || data.isError || geometry.isError) && (
              <div className="v2-error" role="alert">
                <b>Данные не загружены</b>
                <p>{network.error?.message || data.error?.message || geometry.error?.message}</p>
                <button
                  className="v2-outline"
                  onClick={() => {
                    network.refetch();
                    geometry.refetch();
                    data.refetch();
                  }}
                >
                  Повторить
                </button>
                <button
                  className="v2-text"
                  onClick={async () => {
                    await refresh();
                    patch({
                      snapshotId: c.snapshotId,
                      publishedSnapshotId: c.snapshotId,
                      forecastId: c.forecastId || undefined,
                      scenarioId: undefined,
                    });
                  }}
                >
                  Открыть доступный снимок
                </button>
              </div>
            )}
            {!view.routeIds.length && (
              <div className="v2-empty v2-overlay">
                <h2>Выберите маршруты</h2>
                <p>Маршруты без географии тоже доступны в аналитике.</p>
              </div>
            )}
            {view.routeIds.length > 0 && data.isFetching && (
              <div className="v2-loading">Загружаем выбранный период…</div>
            )}
          </div>
          <div className="v2-timeline">
            <div className="v2-time-heading">
              <div>
                <b>
                  {frame ? intervalLabel(frame.start, frame.end, scope.grain) : 'Выберите период'}
                </b>
                <span>Московское время · {periodLabel}</span>
                {c.forecastOptions?.find((f) => f.id === (view.forecastId ?? c.forecastId))
                  ?.kind === 'annual_scenario' && (
                  <span className="annual-forecast-note">Годовой сценарий · невалидированный</span>
                )}
              </div>
              <div className="v2-segmented">
                {([1, 12, 24] as const).map((hours) => (
                  <button
                    key={hours}
                    aria-pressed={view.windowHours === hours}
                    onClick={() => patch({ windowHours: hours })}
                  >
                    {hours === 1 ? 'Час' : hours === 12 ? '12 часов' : 'Весь день'}
                  </button>
                ))}
              </div>
              <button className="v2-outline" onClick={() => setDateOpen(true)}>
                <CalendarDays size={16} />
                Дата и время
              </button>
            </div>
            <div className="v2-slider-row">
              <button
                aria-label={playing ? 'Пауза' : 'Воспроизвести'}
                disabled={!!view.scenarioRange || !frame || index === lastHour}
                onClick={() => {
                  if (view.windowHours === 24) setView((v) => ({ ...v, windowHours: 1 }));
                  setPlaying(!playing);
                }}
              >
                {playing ? <Pause size={18} /> : <Play size={18} />}
              </button>
              <select
                aria-label="Скорость воспроизведения"
                value={speed}
                onChange={(e) => setSpeed(Number(e.target.value))}
              >
                <option value="0.5">0,5×</option>
                <option value="1">1×</option>
                <option value="2">2×</option>
              </select>
              <input
                type="range"
                aria-label="Выбранное время"
                aria-valuetext={
                  frame
                    ? `${intervalLabel(frame.start, frame.end, scope.grain)}, московское время`
                    : ''
                }
                min="0"
                max={lastHour}
                value={index}
                disabled={!!view.scenarioRange || !frame}
                onPointerDown={() => {
                  if (view.windowHours === 24) patch({ windowHours: 1 });
                }}
                onKeyDown={(e) => {
                  if (
                    view.windowHours === 24 &&
                    [
                      'ArrowLeft',
                      'ArrowRight',
                      'ArrowUp',
                      'ArrowDown',
                      'Home',
                      'End',
                      'PageUp',
                      'PageDown',
                    ].includes(e.key)
                  )
                    patch({ windowHours: 1 });
                }}
                onChange={(e) =>
                  patch({
                    index: Number(e.target.value),
                    windowHours: view.windowHours === 24 ? 1 : view.windowHours,
                  })
                }
              />
              <span>{String(index).padStart(2, '0')}:00</span>
            </div>
            <p className="v2-hint">Ползунок меняет время в пределах выбранного дня.</p>
          </div>
          <button
            className="v2-collapse"
            aria-expanded={analyticsOpen}
            onClick={() => setAnalyticsOpen(!analyticsOpen)}
          >
            {analyticsOpen ? <ChevronDown size={16} /> : <ChevronUp size={16} />}Аналитика маршрутов
          </button>
          {analyticsOpen && data.data && view.routeIds.length > 0 && (
            <Analytics
              passengerScope={sectionScope}
              data={data.data}
              scope={scope}
              frame={frame}
              tab={tab}
              onTab={setTab}
              onSelect={(id, i) => {
                select({ kind: 'route', id });
                if (i !== undefined && data.data?.frames[i]) {
                  patch(hourViewAt(data.data.frames[i].start));
                }
              }}
            />
          )}
        </section>
        {right && !toolsPanel && (
          <aside ref={detailsPanel} className="v2-details" aria-label="Сведения об объекте">
            <div className="v2-panel-heading">
              <span className="v2-eyebrow">
                {selectedStop
                  ? 'ОСТАНОВКА СПРАВОЧНИКА'
                  : selectedSegment
                    ? 'УЧАСТОК · МОДЕЛЬ'
                    : 'МАРШРУТ'}
              </span>
              <button aria-label="Скрыть сведения" onClick={() => setRight(false)}>
                <PanelRightClose size={17} />
              </button>
            </div>
            <span className="v2-route-number">{focused}</span>
            <h2>
              {selectedSegment
                ? `${sectionLoad?.fromName ?? networkData?.stops.find((s) => s.id === selectedSegment.fromId)?.name ?? 'Остановка'} → ${sectionLoad?.toName ?? networkData?.stops.find((s) => s.id === selectedSegment.toId)?.name ?? 'Остановка'}`
                : (selectedStop?.name ?? route?.name ?? `Маршрут № ${focused}`)}
            </h2>
            {!DEMO && !selectedSegment && (
              <InfrastructureDetails
                snapshotId={view.snapshotId}
                date={mapDate}
                route={focused}
                stopId={selectedStop?.id}
                radius={infrastructureRadius}
              />
            )}
            {selectedSegment ? (
              <SectionDetails
                load={sectionLoad}
                directionName={
                  networkData?.patterns.find((p) => p.id === selectedSegment.patternId)?.name
                }
                thresholds={thresholds}
                onRoute={() => select({ kind: 'route', id: focused })}
              />
            ) : selectedStop ? (
              <>
                <p className="v2-notice">Отдельный числовой прогноз остановки недоступен.</p>
                <dl>
                  <dt>Идентификатор остановки</dt>
                  <dd>{selectedStop.stationId}</dd>
                  <dt>Позиция в шаблоне</dt>
                  <dd>{selectedStop.order}</dd>
                </dl>
                <button
                  className="v2-outline"
                  onClick={() => select({ kind: 'route', id: focused })}
                >
                  Показать значения всего маршрута
                </button>
              </>
            ) : (
              <>
                <p className="v2-muted">
                  {route?.hasGeometry
                    ? route.sourceKind === 'user_csv'
                      ? 'Трасса из CSV пользователя'
                      : 'Трасса по трамвайным путям'
                    : 'География отсутствует; маршрут учтён в числах'}
                </p>
                <div className="v2-detail-metric">
                  <span>Успешные валидации</span>
                  <strong>{number(value?.value)}</strong>
                  <small>
                    {frame
                      ? intervalLabel(frame.start, frame.end, scope.grain)
                      : 'Нет выбранного интервала'}{' '}
                    · МСК
                  </small>
                </div>
                <dl>
                  <dt>Активных вагонов · средняя оценка</dt>
                  <dd data-testid="fleet-vehicles">{number(value?.fleetVehicles)}</dd>
                  <dt>Валидаций на вагон в час</dt>
                  <dd data-testid="fleet-load">{number(value?.loadPerVehicleHour)}</dd>
                </dl>
                <p className="v2-hint" data-testid="fleet-source">
                  {fleetSourceLabel(value?.fleetSource)}. Вагоны без валидаций могут не попасть в
                  расчёт.
                </p>
                {data.data?.meta.fleetMethod === 'mean_garage_activity_5min_short_gaps_20min' && (
                  <p className="v2-hint" data-testid="fleet-method">
                    Среднее по 5-минутным интервалам. Между отметками с разрывом до 20 минут
                    предполагается присутствие вагона. Праздники и рабочие субботы учитываются в
                    прогнозном профиле. Это оценка активности, не подтверждённый выпуск.
                  </p>
                )}
                {!DEMO && (
                  <PassengerComposition compact scope={{ ...sectionScope, routeIds: [focused] }} />
                )}
                <RouteSections
                  loads={Object.values(sectionLoads).filter(
                    (s) =>
                      s.routeId === focused &&
                      (geometry.data?.segmentIds.includes(s.segmentId) ?? false),
                  )}
                  thresholds={thresholds}
                  onSelect={(s) => select({ kind: 'segment', id: s.segmentId, routeId: s.routeId })}
                />
                <dl>
                  <dt>Историческое среднее</dt>
                  <dd>{number(value?.baseline)}</dd>
                  <dt>Количество сопоставимых недель</dt>
                  <dd>{number(value?.baselineCount)}</dd>
                  <dt>Отклонение от среднего</dt>
                  <dd>
                    {value?.value != null && value.baseline != null
                      ? number(value.value - value.baseline)
                      : 'Нет данных'}
                  </dd>
                </dl>
                {focused === '5' && (
                  <p className="v2-notice">
                    Нет положительной истории. Нулевой прогноз — явно выбранная резервная политика.
                  </p>
                )}
                {!!value?.qualityFlags.length && (
                  <p className="v2-notice">
                    Период требует проверки качества. Официальный target сохранён без исправлений.
                  </p>
                )}
                {patterns.length > 0 && (
                  <details className="v2-route-stops">
                    <summary>Остановки маршрута</summary>
                    {patterns
                      .filter((p) => !selectedPattern || p.id === selectedPattern)
                      .map((pattern) => (
                        <div key={pattern.id}>
                          <h3>{pattern.name}</h3>
                          {pattern.sourceKind === 'user_csv' && (
                            <p className="v2-hint">
                              CSV · с {pattern.validFrom} до {pattern.validTo} (не включительно)
                            </p>
                          )}
                          <ol>
                            {networkData?.stops
                              .filter((s) => s.patternId === pattern.id)
                              .map((stop) => (
                                <li key={stop.id}>
                                  <button onClick={() => select({ kind: 'stop', id: stop.id })}>
                                    {stop.name}
                                  </button>
                                </li>
                              ))}
                          </ol>
                        </div>
                      ))}
                  </details>
                )}
              </>
            )}
            <div className="v2-provenance">
              <h3>Происхождение данных</h3>
              <p>{sourceLabel(value?.provenance)}</p>
              <p>{data.data?.meta.coverageNote}</p>
              {data.data?.meta.issuedAt &&
                (value?.provenance === 'forecast' || value?.provenance === 'mixed') && (
                  <>
                    <span>Модельный момент выпуска</span>
                    <b>{dateLabel(data.data.meta.issuedAt)}</b>
                  </>
                )}
              <code>
                {value?.provenance === 'observation'
                  ? data.data?.meta.historyId
                  : data.data?.meta.modelId}
              </code>
              <p className="v2-hint">
                Одна успешная валидация — запись в выгрузке. Это не число людей в вагоне или на
                остановке.
              </p>
            </div>
            <div className="v2-provenance">
              <h3>Источник карты</h3>
              <p>Маршруты на {mapDate} · версия геометрии по дате</p>
              <p>Версия OSM от {networkData?.sourceAsOf ?? networkData?.asOf}</p>
              {route?.sourceKind === 'reconstruction' && (
                <p className="v2-notice">{route.geometryNote}</p>
              )}
              {patterns.some((p) => p.sourceKind === 'user_csv') && (
                <p className="v2-notice">Направление загружено из CSV пользователем.</p>
              )}
              <p className="v2-hint">{networkData?.warning}</p>
              {(selectedStop?.sourceUrl || route?.sourceUrl) && (
                <a
                  href={selectedStop?.sourceUrl || route?.sourceUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  {selectedStop ? 'Остановка' : 'Маршрут'} в OpenStreetMap ↗
                </a>
              )}
              {networkData?.officialMapUrl && (
                <p>
                  <a href={networkData.officialMapUrl} target="_blank" rel="noreferrer">
                    Официальная схема Москвы ↗
                  </a>
                </p>
              )}
            </div>
            {!!geometry.data?.missingGeometryRouteIds.length && (
              <p className="v2-notice">
                Без географии на выбранную дату: №{' '}
                {geometry.data.missingGeometryRouteIds.join(', ')}. Они включены в итоги.
              </p>
            )}
          </aside>
        )}
      </div>
      {uploadOpen && networkData && (
        <RouteUpload
          network={networkData}
          snapshotId={view.snapshotId}
          routeId={focused}
          onClose={() => setUploadOpen(false)}
          onApplied={(result) => {
            setUploadOpen(false);
            setToolsPanel('');
            setShowReference(true);
            select({ kind: 'route', id: result.routeId });
            patch({
              snapshotId: result.snapshotId,
              publishedSnapshotId: result.snapshotId,
              date: result.validFrom,
              windowHours: 24,
              index: 0,
              geometry: { ...view.geometry, patternIds: [], section: null },
            });
            void refresh();
            notify('Маршрут сохранён. Открыт первый день действия версии.');
          }}
        />
      )}
      <footer className="v2-footer">
        <span>
          <Activity size={12} /> {sourceLabel(frameSource(frame))} · факт при наличии, иначе прогноз
        </span>
        <button
          onClick={async () => {
            await refresh();
            notify('Список доступных данных обновлён.');
          }}
        >
          <RefreshCw size={12} />
          Проверить выпуски
        </button>
        {c.snapshotId !== (view.publishedSnapshotId ?? view.snapshotId) && (
          <button
            onClick={() =>
              patch({
                snapshotId: c.snapshotId,
                publishedSnapshotId: c.snapshotId,
                forecastId: c.forecastId || undefined,
                scenarioId: undefined,
                date: c.defaultDate,
              })
            }
          >
            Открыть новый снимок
          </button>
        )}
      </footer>
      {toast && (
        <div className="v2-toast" role="status">
          {toast}
        </div>
      )}
      {dateOpen && (
        <DatePicker
          view={view}
          capabilities={c}
          onClose={() => setDateOpen(false)}
          onApply={patch}
        />
      )}
      {draft && !DEMO && (
        <ExportPanel
          scope={draft.scope}
          initialForecastId={view.forecastId ?? data.data?.meta.forecastId ?? c.forecastId}
          timeRange={draft.timeRange}
          onClose={() => setDraft(null)}
        />
      )}
      {draft && DEMO && (
        <ExportDialog
          draft={draft}
          canSubmit={c.submissionAvailable && !DEMO}
          onClose={() => setDraft(null)}
        />
      )}
      {help && (
        <Modal title="О данных и ограничениях" onClose={() => setHelp(false)}>
          <p>
            Текущие периоды истории и прогноза определяются опубликованным выпуском. Новые данные и
            модели доступны в панели «Данные и модели».
          </p>
          <p>Источник выбирается автоматически: фактические данные при наличии, иначе прогноз.</p>
          <p>
            Показатель — число успешных валидаций по маршруту и часу. Он не измеряет наполнение
            вагона, присутствующих на остановке или фактические посадки всех пассажиров.
          </p>
          <p>
            Карта содержит трамвайные пути и остановки всех десяти целевых маршрутов из
            OpenStreetMap. Направление, участок и границы карты не меняют числовые суммы.
          </p>
          <p>
            В новом расчёте число вагонов усредняется по 5-минутным интервалам. Разрывы между
            отметками одного вагона до 20 минут заполняются, если на другом маршруте нет его
            отметок. Для прогноза используется медиана за восемь предшествующих недель: рабочая
            суббота сравнивается с пятницей, праздник — с воскресеньем. Это допущение по
            производственному календарю, а не подтверждённое расписание перевозчика. Для 12 часов и
            дня суммируются вагоно-часы. Точные сведения о выпуске пока не подключены.
          </p>
          <p>
            Цвет участка — отдельная модельная оценка, не наблюдение на остановке. Маршрутные
            валидации распределяются по возможным парам остановок в порядке движения. Утром
            предполагается больший спрос к пересадочным узлам, вечером — от них; короткие поездки
            получают больший вес. Число вагонов распределяется между направлениями пропорционально
            длине трасс. Параметры пока не обучены на остановочных данных. Стрелки показывают
            направление; нажмите на перегон для его оценки.
          </p>
          {!DEMO && (
            <div data-testid="service-dataset">
              <p>
                Датасет на каждый день 2025 года: 10 маршрутов, 3650 строк. Архив расписаний
                неполный: найдены отправления маршрута 7 за 29 июля в одном направлении и средние
                интервалы на опубликованных схемах. Расчётные значения помечены отдельно; справочные
                интервалы пока не используются для окраски карты.
              </p>
              <p>
                <a href={serviceDatasetUrl('daily_service_2025.csv')}>По дням · CSV</a>
                {' · '}
                <a href={serviceDatasetUrl('hourly_fleet_2025.csv')}>Оценка по часам · CSV</a>
                {' · '}
                <a href={serviceDatasetUrl('README.md')}>Методика и источники</a>
              </p>
            </div>
          )}
          <p>В P0 нет текущего потока, остановочного прогноза и доверительных интервалов.</p>
          <p>
            <a href="/api/v1/data-quality" target="_blank" rel="noreferrer">
              Открыть отчёт качества API ↗
            </a>
          </p>
        </Modal>
      )}
    </main>
  );
}
function DatePicker(p: {
  view: ViewState;
  capabilities: Capabilities;
  onApply: (value: Partial<ViewState>) => void;
  onClose: () => void;
}) {
  const [windowHours, setWindowHours] = useState<WindowHours>(p.view.windowHours),
    [startHour, setStartHour] = useState(p.view.index),
    [date, setDate] = useState(p.view.date),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const bounds = dateBounds(p.capabilities);
  const start = Math.min(startHour, 24 - windowHours);
  const at = Date.parse(`${date}T00:00:00+03:00`);
  const options = p.capabilities.forecastOptions || [];
  const chosen =
    options.find((f) => Date.parse(f.start) <= at && Date.parse(f.end) >= at + 86400000) ||
    options.find((f) => f.id === p.capabilities.forecastId);
  return (
    <Modal title="Дата и время" onClose={p.onClose}>
      <p className="v2-hint">
        История и готовые прогнозы доступны до {bounds.max}. Годовой горизонт считается от момента
        выпуска, а не от текущей даты.
      </p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError('');
          try {
            let snapshotId = p.view.snapshotId;
            const changed =
              chosen && chosen.id !== (p.view.forecastId ?? p.capabilities.forecastId);
            if (changed) {
              const result = await platform(
                '/forecast-map-views',
                z.object({ snapshotId: z.string() }),
                { forecast_id: chosen.id, snapshot_id: snapshotId },
              );
              snapshotId = result.snapshotId;
            }
            p.onApply({
              date,
              windowHours,
              index: start,
              snapshotId,
              publishedSnapshotId: p.view.publishedSnapshotId ?? p.view.snapshotId,
              ...(chosen ? { forecastId: chosen.id } : {}),
              ...(changed ? { scenarioId: undefined } : {}),
            });
            p.onClose();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="v2-field">
          Дата
          <input
            type="date"
            name="date"
            required
            min={bounds.min}
            max={bounds.max}
            value={date}
            onChange={(e) => setDate(e.target.value)}
          />
        </label>
        <label className="v2-field">
          Показать
          <select
            value={windowHours}
            onChange={(e) => setWindowHours(Number(e.target.value) as WindowHours)}
          >
            <option value="24">Весь день</option>
            <option value="12">12 часов</option>
            <option value="1">Час</option>
          </select>
        </label>
        {windowHours < 24 && (
          <label className="v2-field">
            {windowHours === 12 ? 'Начало · МСК' : 'Время · МСК'}
            <select value={start} onChange={(e) => setStartHour(Number(e.target.value))}>
              {Array.from({ length: 25 - windowHours }, (_, h) => (
                <option key={h} value={h}>
                  {String(h).padStart(2, '0')}:00
                </option>
              ))}
            </select>
          </label>
        )}
        <p className="v2-hint">
          Будет показана сумма за {String(start).padStart(2, '0')}:00–
          {String(start + windowHours).padStart(2, '0')}:00 по московскому времени.
        </p>
        {chosen?.kind === 'annual_scenario' && (
          <p className="scenario-note">
            Для этой даты используется годовой сценарий от {chosen.origin.slice(0, 10)}. Он не
            валидирован; погодные условия будущего года неизвестны. Архив геометрии 2025 не
            продлевается автоматически.
          </p>
        )}
        {error && <p role="alert">{error}</p>}
        <button className="v2-primary full" type="submit" disabled={busy}>
          {busy ? 'Открываем прогноз…' : 'Применить'}
        </button>
      </form>
    </Modal>
  );
}

function ExportDialog(p: { draft: Draft; canSubmit: boolean; onClose: () => void }) {
  const [whole, setWhole] = useState(false),
    [submission, setSubmission] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  return (
    <Modal title="Экспорт данных" onClose={p.onClose}>
      <p>{intervalLabel(p.draft.timeRange.start, p.draft.timeRange.end, 'hour')} · МСК.</p>
      <p className="v2-hint">
        CSV содержит почасовые значения выбранного интервала. Их сумма соответствует карте и
        таблице.
      </p>
      <dl>
        <dt>Маршруты</dt>
        <dd>
          {submission
            ? 'Все 10 конкурсных маршрутов'
            : p.draft.scope.routeIds.map((r) => `№ ${r}`).join(', ')}
        </dd>
        <dt>Показатель</dt>
        <dd>Успешные валидации по маршруту</dd>
        <dt>Снимок</dt>
        <dd>
          <code>{p.draft.scope.snapshotId}</code>
        </dd>
      </dl>
      <label className="v2-check">
        <input
          type="checkbox"
          checked={whole}
          disabled={submission || busy}
          onChange={(e) => setWhole(e.target.checked)}
        />
        Весь день · 24 часа
      </label>
      <label className="v2-check">
        <input
          type="checkbox"
          checked={submission}
          disabled={!p.canSubmit || busy}
          onChange={(e) => setSubmission(e.target.checked)}
        />
        Конкурсный submission.csv · 14 640 строк
      </label>
      <p className="v2-hint">
        Географические фильтры не ограничивают маршрутные суммы. Конкурсный файл всегда содержит все
        маршруты и 61 день.
      </p>
      {error && (
        <p className="v2-form-error" role="alert">
          {error}
        </p>
      )}
      <button
        className="v2-primary full"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError('');
          const abort = new AbortController();
          controller.current = abort;
          try {
            await exportData(
              { ...p.draft.scope, timeRange: whole ? p.draft.scope.timeRange : p.draft.timeRange },
              null,
              submission,
              abort.signal,
            );
            p.onClose();
          } catch (e) {
            if (!abort.signal.aborted)
              setError(e instanceof Error ? e.message : 'Не удалось создать экспорт');
          } finally {
            if (!abort.signal.aborted) setBusy(false);
          }
        }}
      >
        <Download size={17} />
        {busy ? 'Готовим файл…' : 'Скачать CSV'}
      </button>
    </Modal>
  );
}
