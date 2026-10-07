import { useEffect } from 'react';
import type { ActivityImpactStreet } from '../../mobile/src/api/generated';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Icon } from './icons';

const count = (value: number | null) => value === null ? '—' : value.toLocaleString();
const HISTORY_MESSAGES = {
  'unknown-dates': 'Some activity dates are missing or invalid. Historical gains are unavailable; supported streets remain visible.',
  'history-limit': 'This history exceeds the comparison limit. Historical gains are unavailable; supported streets remain visible.',
  'missing-provenance': 'This activity has no successful matching record in this region. Its contribution is unavailable.',
  ready: '',
};

export function ActivityImpact({ runtime, selectedStreetId, onFocus, onDetails }: { runtime: Runtime; selectedStreetId: string | null; onFocus: (street: ActivityImpactStreet) => void; onDetails: (street: ActivityImpactStreet) => void }) {
  const state = useStore(runtime.activities); const cities = useStore(runtime.cities);
  useEffect(() => {
    if (state.detail && !state.impactDatasetId && cities.datasets.length) runtime.activities.selectImpactDataset(cities.selectedDatasetId ?? cities.datasets[0].dataset_id);
  }, [runtime, state.detail, state.impactDatasetId, cities.datasets, cities.selectedDatasetId]);
  const impact = state.impact;
  const loading = state.impactStatus === 'loading' || state.impactStatus === 'loading-more';
  return <section className="activity-impact" aria-label="Activity contribution">
    <h3>What this run added</h3>
    <label className="field-label">Contribution region<select aria-label="Contribution region" value={state.impactDatasetId ?? ''} onChange={(event) => runtime.activities.selectImpactDataset(event.target.value || null)}>
      <option value="" disabled>Choose a region</option>{state.impactDatasetId && !cities.datasets.some((dataset) => dataset.dataset_id === state.impactDatasetId) && <option value={state.impactDatasetId}>Selected region</option>}{cities.datasets.map((dataset) => <option key={dataset.dataset_id} value={dataset.dataset_id}>{dataset.region}</option>)}
    </select></label>
    <label className="field-label">Completion rule<select aria-label="Contribution completion rule" value={state.impactRule} onChange={(event) => runtime.activities.selectImpactDataset(state.impactDatasetId, event.target.value as 'normal' | 'strict')}><option value="normal">Normal · 90%, all nodes on small streets</option><option value="strict">Strict · every GPS node</option></select></label>
    {!cities.datasets.length && !state.impactDatasetId && <p className="note">No supported region is available yet.</p>}
    {state.impactStatus === 'loading' && <p className="note" role="status">Loading contribution…</p>}
    {state.impactError && <div className="alert" role="alert">{state.impactError}<button className="text-button" onClick={() => void runtime.activities.retryImpact()}>Retry contribution</button></div>}
    {impact && <>
      <div className="impact-totals"><div data-metric="new-nodes"><strong>{count(impact.new_nodes)}</strong><span>New GPS nodes</span></div><div data-metric="advanced"><strong>{count(impact.streets_advanced)}</strong><span>Streets advanced</span></div><div data-metric="completed"><strong>{count(impact.streets_completed)}</strong><span>Streets completed</span></div></div>
      {impact.coverage.status !== 'ready' ? <p className="note">{impact.coverage.status === 'pending' ? 'Coverage is still processing. Contribution will be available after matching.' : 'Coverage processing failed. Retry the import to calculate contribution.'}<button className="text-button" disabled={loading} onClick={() => void runtime.activities.refreshImpact()}>Refresh contribution</button></p> : <>
        {impact.history_status !== 'ready' && <p className="note">{HISTORY_MESSAGES[impact.history_status]}</p>}
        {impact.supported_nodes !== null && <p className="note">{count(impact.supported_nodes)} GPS nodes supported by this activity.</p>}
        {impact.history_status === 'ready' && impact.new_nodes === 0 && <p className="note">{impact.supported_nodes ? 'Revisited streets: this run added no new GPS nodes in retained history.' : 'No eligible GPS nodes were matched in this region.'}</p>}
        {impact.streets.map((street) => <div className={`impact-street ${selectedStreetId === street.street_id ? 'selected' : ''}`} key={street.street_id}>
          <button className="street-row" aria-label={`Show ${street.name} on map`} onClick={() => onFocus(street)}><span className={`street-state ${street.completed_by_activity ? 'complete' : 'partial'}`}><Icon name={street.completed_by_activity ? 'check' : 'pin'} size={17} /></span><span className="row-copy"><strong>{street.name}</strong><span>{street.city_name} · {street.before_nodes === null ? `${count(street.supported_nodes)} supported / ${street.eligible_nodes} GPS nodes` : `${street.before_nodes} → ${street.after_nodes} / ${street.eligible_nodes} GPS nodes`}</span><span>{street.completed_by_activity ? 'Completed by this run' : street.new_nodes === null ? 'Historical gain unavailable' : street.new_nodes > 0 ? `+${count(street.new_nodes)} new nodes` : 'Revisited'}{street.current_nodes !== null && ` · now ${street.current_nodes} / ${street.eligible_nodes}`}</span></span><Icon name="pin" size={15} /></button>
          <button className="text-button" aria-label={`${street.name} details`} onClick={() => onDetails(street)}>Street details<Icon name="arrow" size={14} /></button>
        </div>)}
        {impact.total !== null && impact.streets.length < impact.total && <button className="secondary full" disabled={loading} onClick={() => void runtime.activities.loadMoreImpact()}>{loading ? 'Loading streets…' : 'More affected streets'}</button>}
      </>}
      <p className="note">Compared with retained activities by recorded date; same-day ordering is approximate. Importing or deleting history can change this comparison. Manual labels add no GPS credit.</p>
    </>}
  </section>;
}
