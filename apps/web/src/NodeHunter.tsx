import { useEffect, useRef, useState } from 'react';
import type { MissingNode } from '../../mobile/src/api/generated';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Icon } from './icons';

export const nodeKey = (node: MissingNode) => `${node.dataset_id}:${node.node_id}`;

export function NodeHunter({ runtime, selected, onSelect, onAdd, onZoom }: { runtime: Runtime; selected: MissingNode | null; onSelect: (node: MissingNode) => void; onAdd: (node: MissingNode) => void; onZoom: () => void }) {
  const state = useStore(runtime.explore); const planner = useStore(runtime.planner);
  const [shown, setShown] = useState(20);
  const selection = useRef<HTMLDivElement>(null);
  const snapshot = state.mapStatus === 'ready' || state.mapStatus === 'empty' ? state.map : null;
  const nodes = snapshot?.missing_nodes ?? [];
  useEffect(() => { setShown(20); }, [snapshot]);
  useEffect(() => { selection.current?.scrollIntoView({ block: 'nearest' }); }, [selected?.dataset_id, selected?.node_id]);
  const zoomed = (state.viewport?.zoom ?? 0) >= 16;
  return <section className="node-hunter" aria-label="Node Hunter">
    <div className="section-title"><h2>Node Hunter</h2><span className="pill">{snapshot && zoomed ? `${snapshot.limits.missing_nodes.truncated ? 'At least ' : ''}${nodes.length.toLocaleString()} in view` : 'Visible area'}</span></div>
    <p className="note">Find original GPS nodes you haven’t visited. GPS gaps can remain on normally or manually completed streets.</p>
    {state.mapError ? <div className="alert" role="alert">{state.mapError}<button className="text-button" onClick={() => void runtime.explore.retry()}>Retry</button></div> : state.mapStatus === 'loading' ? <p className="note" role="status">Updating missing nodes for this view…</p> : !zoomed ? <><p className="note">Zoom closer to find individual missing nodes.</p><button className="secondary full" onClick={onZoom}>Zoom to missing nodes</button></> : !snapshot || snapshot.geography_state === 'geography_pending' ? <p className="note">Missing-node coverage is not available in this area yet.</p> : <>
      {snapshot.node_state === 'pending' && <p className="note">Some coverage is unavailable or still processing. Only nodes from ready datasets appear.</p>}
      {snapshot.limits.missing_nodes.truncated && <p className="note">This view is limited to {snapshot.limits.missing_nodes.limit.toLocaleString()} missing nodes. Zoom in to see a smaller area.</p>}
      {selected && <div className="hunter-selection" ref={selection}><strong>Missing node {selected.node_id}</strong><span>{selected.latitude.toFixed(6)}, {selected.longitude.toFixed(6)}</span><button className="primary full" disabled={planner.waypoints.length >= 20} onClick={() => onAdd(selected)}><Icon name="plus" size={16} />Add node to route</button>{planner.waypoints.length >= 20 && <p className="note">Your route already has 20 waypoints. Remove one to add this node.</p>}</div>}
      {!nodes.length && <p className="note">{snapshot.node_state === 'pending' ? 'No missing nodes in the ready coverage shown so far.' : 'No missing GPS nodes in the supported area of this view.'}</p>}
      <div className="hunter-list">{nodes.slice(0, shown).map((node) => <button key={nodeKey(node)} className={`node-row ${selected && nodeKey(selected) === nodeKey(node) ? 'selected' : ''}`} aria-pressed={Boolean(selected && nodeKey(selected) === nodeKey(node))} onClick={() => onSelect(node)}><Icon name="pin" size={16} /><span>Node {node.node_id}<small>{node.latitude.toFixed(5)}, {node.longitude.toFixed(5)}</small></span><Icon name="arrow" size={14} /></button>)}</div>
      {shown < nodes.length && <button className="secondary full" onClick={() => setShown(shown + 20)}>Show more nodes</button>}
    </>}
  </section>;
}
