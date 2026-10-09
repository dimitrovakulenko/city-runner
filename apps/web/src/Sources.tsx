import { useEffect } from 'react';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { FILE_ERRORS } from './Imports';
import { Icon } from './icons';

const recordedDate = (value: string | null) => value ? new Date(`${value}T12:00:00`).toLocaleDateString() : 'Unknown';
const importedAt = (value: string | null) => value ? new Date(value).toLocaleString() : 'No successful file imports yet';

export function Sources({ runtime, online, onImports, onActivity }: {
  runtime: Runtime; online: boolean; onImports: () => void; onActivity: (id: string) => void;
}) {
  const store = runtime.sync; const state = useStore(store); const data = state.data;
  const token = data?.change_token;
  useEffect(() => { void store.loadFailures(store.getState().failures?.page ?? 1); }, [store, token, state.eligible]);
  const refresh = () => { void store.retryRefresh(); };
  return <>
    <div className="panel-heading"><span className="eyebrow">YOUR ACTIVITY SOURCES</span><h1>Sources & sync</h1><p>See how your history is importing and when coverage is ready.</p></div>
    <section aria-label="File import status">
      <div className="section-title"><h2>File imports</h2><button className="icon-button" aria-label="Refresh source status" disabled={!online || state.refreshing} onClick={refresh}><Icon name="refresh" size={17} /></button></div>
      <p className="note" role="status">{!online ? 'Offline · showing last loaded source status.' : !state.eligible ? 'Automatic refresh pauses while this tab is hidden.' : state.pollingStopped ? 'Automatic refresh paused. Retry refresh to continue.' : state.refreshing ? 'Checking your sources…' : state.error ? 'Refresh delayed. Retry when connected.' : 'Automatic refresh is active while this tab is visible.'}</p>
      {state.error && <div className="alert" role="alert">{state.error}<button className="text-button" disabled={!online} onClick={refresh}>Retry refresh</button></div>}
      {data ? <>
        <div className="source-history"><strong>{data.activity_count.toLocaleString()} {data.activity_count === 1 ? 'activity' : 'activities'}</strong><span>Account-wide retained history</span><dl><dt>Oldest known recording</dt><dd>{recordedDate(data.oldest_activity_date)}</dd><dt>Last file import</dt><dd>{importedAt(data.last_import_at)}</dd></dl></div>
        {(['gpx', 'fit'] as const).map((kind) => {
          const files = data.files[kind];
          return <article className="source-card" key={kind}><h3>{kind.toUpperCase()} files</h3><dl className="source-counts"><dt>Imported</dt><dd>{files.imported}</dd><dt>Waiting to process</dt><dd>{files.queued}</dd><dt>Processing</dt><dd>{files.processing}</dd><dt>Needs attention</dt><dd>{files.failed}</dd>{files.unavailable > 0 && <><dt>Status unavailable</dt><dd>{files.unavailable}</dd></>}</dl></article>;
        })}
        <article className="source-card"><h3>Coverage processing</h3><dl className="source-counts"><dt>Ready</dt><dd>{data.coverage.ready}</dd><dt>Pending</dt><dd>{data.coverage.pending}</dd><dt>Needs attention</dt><dd>{data.coverage.failed}</dd><dt>Geography unavailable</dt><dd>{data.coverage.unavailable}</dd></dl><p className="note">These are imported file counts. Coverage is ready after all supported regions finish matching.</p></article>
        {(data.batches.waiting > 0 || data.batches.stopped > 0) && <p className="pause-note">{data.batches.waiting} files still need uploading · {data.batches.stopped} stopped batches. Accepted jobs continue processing. Unsubmitted files require selection in Imports.</p>}
        {(data.batches.duplicates > 0 || data.batches.deleted > 0) && <p className="note">{data.batches.duplicates} duplicate entries · {data.batches.deleted} deleted entries. Deleted items stay deleted.</p>}
      </> : <p className="note">{state.status === 'loading' ? 'Loading source status…' : 'Source status is unavailable. Retry when connected.'}</p>}
      <button className="secondary full" disabled={!online} onClick={onImports}><Icon name="upload" size={17} />Open file imports</button>
    </section>
    <section aria-label="Source failures">
      <div className="section-title"><h2>Needs attention</h2><button className="text-button" disabled={!online || state.failuresLoading} onClick={() => void store.loadFailures(state.failures?.page ?? 1)}>Refresh failures</button></div>
      {state.failureError && <div className="alert" role="alert">{state.failureError}</div>}
      {state.failuresLoading && <p className="note">Checking failed imports…</p>}
      {state.failures?.items.map((item) => {
        const corrupt = item.stage === 'import' && FILE_ERRORS.has(item.error ?? '');
        return <article className="source-card source-failure" key={`${item.source_id}:${item.stage}`}><h3>{item.file_kind.toUpperCase()} · {item.stage === 'coverage' ? 'Coverage processing failed' : 'File processing failed'}</h3><p className="note">{corrupt ? 'Choose a corrected file and start a new import.' : item.stage === 'coverage' ? 'Your original activity is imported. Retry its coverage calculation.' : 'The accepted file could not be processed. Retry when connected.'}</p><div className="filter-actions">{corrupt ? <button className="text-button" disabled={!online} onClick={onImports}>Choose corrected file</button> : <button className="secondary" disabled={!online || !state.eligible || state.refreshing || state.retrying !== null} onClick={() => void store.retryFailure(item)}>{state.retrying === item.source_id ? 'Retrying…' : item.stage === 'coverage' ? 'Retry coverage' : 'Retry file processing'}</button>}{item.activity_id && <button className="text-button" onClick={() => onActivity(item.activity_id!)}>Open activity</button>}</div></article>;
      })}
      {state.failures?.total === 0 && !state.failuresLoading && <p className="note">No failed source jobs.</p>}
      {state.failures && state.failures.total > state.failures.page_size && <div className="filter-actions"><button className="text-button" disabled={!online || state.failuresLoading || state.failures.page === 1} onClick={() => void store.loadFailures(state.failures!.page - 1)}>Previous failures</button><span className="note">Page {state.failures.page}</span><button className="text-button" disabled={!online || state.failuresLoading || state.failures.page * state.failures.page_size >= state.failures.total} onClick={() => void store.loadFailures(state.failures!.page + 1)}>Next failures</button></div>}
    </section>
    <section aria-label="Cloud connections"><div className="section-title"><h2>Cloud connections</h2></div>{data?.providers.map((provider) => <article className="source-card" key={provider.provider}><div className="batch-heading"><h3>{provider.provider === 'garmin' ? 'Garmin' : 'Strava'}</h3><span className="pill muted">Unavailable</span></div><p className="note">{provider.reason}</p></article>)}<p className="note">File imports update this map. Automatic Garmin and Strava imports are not available yet.</p></section>
  </>;
}
