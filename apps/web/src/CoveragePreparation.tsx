import type { Runtime } from './runtime';
import { useStore } from './hooks';

export function CoveragePreparation({ runtime, online }: { runtime: Runtime; online: boolean }) {
  const state = useStore(runtime.preparation); const data = state.data;
  if (!state.error && (!data?.enabled || data.state === 'ready')) return null;
  const retry = async () => {
    const generation = runtime.preparation.getAccountGeneration();
    if (await runtime.preparation.retry() && generation === runtime.preparation.getAccountGeneration()) void runtime.sync.retryRefresh();
  };
  return <section className="source-card" aria-label="City preparation">
    <h3>{data?.state === 'failed' ? 'City preparation needs attention' : data?.state === 'unsupported' ? 'Some locations need city coverage' : 'Preparing city coverage'}</h3>
    <p className="note" role="status">{!online ? 'Offline · showing last loaded city preparation.' : data?.state === 'failed' ? 'Your activities are saved. Retry preparing their cities to calculate coverage.' : data?.state === 'unsupported' ? 'Some activity locations do not have supported city coverage yet. Your activities are saved.' : 'We’re finding and preparing the cities touched by your activities. Coverage appears automatically when they’re ready.'}</p>
    {data?.cities.filter((city) => city.state !== 'ready').map((city) => <p className="note" key={city.osm_relation_id}>{city.state === 'failed' ? `${city.name} preparation failed` : `Preparing ${city.name} coverage`}</p>)}
    {data?.cities_truncated && <p className="note">More cities are being prepared.</p>}
    {state.error && <div className="alert" role="alert">{state.error}<button className="text-button" disabled={!online || state.status === 'loading'} onClick={() => void runtime.sync.retryRefresh()}>Retry city status</button></div>}
    {data?.state === 'failed' && <button className="secondary full" disabled={!online || state.retrying || state.status === 'loading'} onClick={() => void retry()}>{state.retrying ? 'Retrying city preparation…' : 'Retry city preparation'}</button>}
  </section>;
}
