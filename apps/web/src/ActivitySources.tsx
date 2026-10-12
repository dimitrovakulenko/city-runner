import { useEffect, useState } from 'react';
import type { ActivitySourceResponse } from '../../mobile/src/api/generated';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Modal } from './Modal';

export function ActivitySources({ runtime, activityId }: { runtime: Runtime; activityId: string }) {
  const state = useStore(runtime.activitySources);
  const corrections = useStore(runtime.corrections);
  const [removing, setRemoving] = useState<ActivitySourceResponse | null>(null);
  const busy = state.removingSourceId !== null || corrections.pending !== null;
  const loading = state.status === 'loading' || state.status === 'loading-more';
  useEffect(() => {
    if (!busy && removing && (state.status === 'ready' || state.status === 'empty') && !state.items.some((source) => source.id === removing.id)) setRemoving(null);
  }, [busy, removing, state.status, state.items]);
  if (state.activityId !== activityId || state.activityMissing) return null;
  return <section className="activity-sources" aria-label="Imported files">
    <h3>Imported files</h3>
    <p className="note">{state.total.toLocaleString()} imported {state.total === 1 ? 'file' : 'files'}. Equivalent recordings share one activity.</p>
    {state.items.map((source) => <div className="import-source-row" key={source.id}>
      <div><strong>{source.source_kind.toUpperCase()} file</strong><span className="note">Imported {new Date(source.created_at).toLocaleString()}</span></div>
      <button className="danger text-button" aria-label={`Remove ${source.source_kind.toUpperCase()} import ${source.id}`} disabled={busy || loading} onClick={() => setRemoving(source)}>Remove import</button>
    </div>)}
    {loading && <p className="note" role="status">Loading imported files…</p>}
    {state.removingSourceId && <p className="note" role="status">Removing import…</p>}
    {state.status === 'empty' && <p className="note">This activity has no retained file imports.</p>}
    {state.error && <div className="alert" role="alert">{state.error}<button className="text-button" disabled={busy} onClick={() => void runtime.activitySources.refresh()}>Retry imported files</button></div>}
    {state.items.length < state.total && <button className="secondary full" disabled={busy || loading} onClick={() => void runtime.activitySources.loadMore()}>Load more imported files</button>}
    {removing && <Modal labelledBy="remove-import-title" onClose={() => { if (!busy) setRemoving(null); }}>
      <h2 id="remove-import-title">Remove this import?</h2>
      <p>This {removing.source_kind.toUpperCase()} file and its coverage support will be removed. Other imports keep this activity. Removing its last import also removes the activity and its coverage.</p>
      <div className="modal-actions"><button className="secondary" disabled={busy} onClick={() => setRemoving(null)}>Keep import</button><button className="primary destructive" disabled={busy} onClick={() => {
        const generation = runtime.activitySources.getAccountGeneration();
        void runtime.activitySources.remove(removing.id).then((accepted) => {
          if (accepted && generation === runtime.activitySources.getAccountGeneration() && runtime.activitySources.getState().activityId === activityId) setRemoving(null);
        });
      }}>Remove import</button></div>
      {state.error && <div className="alert" role="alert">{state.error}</div>}
    </Modal>}
  </section>;
}
