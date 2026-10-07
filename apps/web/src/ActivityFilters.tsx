import { useEffect, useState } from 'react';
import type { ActivityFilters as Filters } from '../../mobile/src/api/generated';
import { canonicalizeActivityFilters } from '../../mobile/src/api/client';
import type { Runtime } from './runtime';
import { useStore } from './hooks';

export function ActivityFilters({ runtime }: { runtime: Runtime }) {
  const activities = useStore(runtime.activities); const explore = useStore(runtime.explore);
  const [draft, setDraft] = useState(activities.filters);
  const [scope, setScope] = useState(explore.coverageScope);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { setDraft(activities.filters); setError(null); }, [activities.filters]);
  useEffect(() => setScope(explore.coverageScope), [explore.coverageScope]);
  const change = (patch: Filters) => setDraft((current) => ({ ...current, ...patch }));
  const apply = () => {
    if (draft.date_from && draft.date_to && draft.date_from > draft.date_to) { setError('The end date must be on or after the start date.'); return; }
    setError(null); runtime.applyFilters(draft); runtime.explore.setCoverageScope(scope);
  };
  const summary = [activities.filters.date_from ? `From ${activities.filters.date_from}` : '', activities.filters.date_to ? `Through ${activities.filters.date_to}` : '', activities.filters.activity_type ?? '', activities.filters.source === 'all' ? '' : activities.filters.source.toUpperCase()].filter(Boolean).join(' · ');
  const types = [...new Set([...(activities.filterOptions?.activity_types ?? []), ...(draft.activity_type ? [draft.activity_type] : []), 'unknown'])];
  return <section className="activity-filters" aria-label="Activity and map filters">
    <details><summary>Activity & map filters</summary><form onSubmit={(event) => { event.preventDefault(); apply(); }}>
      <div className="filter-dates"><label className="field-label">From<input aria-label="Activity start date" type="date" value={draft.date_from ?? ''} onChange={(event) => change({ date_from: event.target.value || null })} /></label><label className="field-label">Through<input aria-label="Activity end date" type="date" value={draft.date_to ?? ''} onChange={(event) => change({ date_to: event.target.value || null })} /></label></div>
      <label className="field-label">Activity type<select aria-label="Activity type" value={draft.activity_type ?? ''} onChange={(event) => change({ activity_type: event.target.value || null })}><option value="">All types</option>{types.map((type) => <option key={type} value={type}>{type === 'unknown' ? 'Unknown type' : type}</option>)}</select></label>
      <label className="field-label">File source<select aria-label="File source" value={draft.source} onChange={(event) => change({ source: event.target.value as Required<Filters>['source'] })}><option value="all">All sources</option><option value="gpx">GPX</option><option value="fit">FIT</option><option value="unknown">Unknown source</option></select></label>
      <label className="field-label">Map coverage<select aria-label="Map coverage" value={scope} onChange={(event) => setScope(event.target.value as typeof scope)}><option value="lifetime">Lifetime GPS coverage</option><option value="filtered">Selected activities’ GPS coverage</option></select></label>
      <p className="note">Filters select activities and map tracks. Choose whether coverage uses your entire history or this selection.</p>
      {activities.filterOptionsError && <p className="note">Activity types could not load. <button type="button" className="text-button" onClick={() => void runtime.activities.retryFilterOptions()}>Retry types</button></p>}
      {activities.filterOptions?.types_truncated && <p className="note">The type list is limited to 100 types.</p>}
      {error && <p className="alert" role="alert">{error}</p>}
      <div className="filter-actions"><button className="primary compact" type="submit">Apply filters</button><button className="text-button" type="button" onClick={() => { setDraft(canonicalizeActivityFilters()); setScope('lifetime'); setError(null); runtime.applyFilters(canonicalizeActivityFilters()); runtime.explore.setCoverageScope('lifetime'); }}>Reset filters</button></div>
    </form></details>
    <p className="filter-summary" role="status">{summary || 'All activities'}<br /><strong>{explore.coverageScope === 'filtered' ? 'Selected activities’ GPS coverage' : 'Lifetime GPS coverage'}</strong></p>
  </section>;
}
