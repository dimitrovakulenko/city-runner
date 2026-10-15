import { useStore } from './hooks';
import type { RouteFollowingStore } from './routeFollowing';

export function RouteFollowingControls({ store }: { store: RouteFollowingStore }) {
  const state = useStore(store);
  return <div className="route-following" aria-label="Route following" role="region" data-course={state.course ?? ''}>
    <span role="status">{state.message}{state.position && ` · ±${Math.round(state.position.accuracy)} m`}</span>
    <div className="following-actions">
      {state.status === 'paused' ? <button className="secondary" onClick={() => store.start()}>Resume following</button> : ['lost', 'denied', 'error'].includes(state.status) ? <button className="secondary" onClick={() => store.start()}>Retry GPS</button> : <button className="secondary" disabled={!state.position || state.centered} onClick={() => store.setCentered(true)}>Recenter route</button>}
      <button className="secondary" onClick={() => store.stop()}>Stop following</button>
    </div>
  </div>;
}
