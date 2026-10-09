import { useRef, useState } from 'react';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Icon } from './icons';
import { prepareImportSelection } from '../../mobile/src/importSelection';

export const FILE_ERRORS = new Set(['invalid_fit_size', 'invalid_fit_file', 'fit_not_activity', 'fit_no_track_points', 'fit_record_limit', 'fit_message_limit', 'fit_multiple_streams', 'invalid_gpx_size', 'invalid_gpx_xml', 'invalid_gpx_root', 'invalid_gpx_coordinate', 'gpx_point_limit', 'gpx_no_track_points']);
const STATUS = { queued: 'Waiting to process', processing: 'Processing activity', succeeded: 'Imported', failed: 'Needs attention', deleted: 'Deleted' };

export function Imports({ runtime, onOpenActivity }: { runtime: Runtime; onOpenActivity: (id: string) => void }) {
  const state = useStore(runtime.imports);
  const input = useRef<HTMLInputElement>(null);
  const target = useRef<{ batchId: string; itemId: string } | null>(null);
  const selectionGeneration = useRef(0);
  const creating = useRef(false); const [preparing, setPreparing] = useState(false);
  const choose = (item?: { batchId: string; itemId: string }) => {
    if (creating.current) return;
    target.current = item ?? null; selectionGeneration.current = runtime.imports.getAccountGeneration();
    if (input.current) { input.current.multiple = !item; input.current.value = ''; input.current.click(); }
  };
  const accept = (files: File[]) => {
    if (creating.current || selectionGeneration.current !== runtime.imports.getAccountGeneration()) return;
    if (target.current) {
      const item = target.current; const file = files[0]; if (!file) return;
      const selection = runtime.files.register(file);
      void runtime.imports.reselect(item.batchId, item.itemId, selection).then((accepted) => { if (!accepted) runtime.files.release(selection.uri); });
    } else {
      const selections = files.map((file) => runtime.files.register(file));
      const accepted = new Set(prepareImportSelection(selections).accepted.map((file) => file.uri));
      for (const file of selections) if (!accepted.has(file.uri)) runtime.files.release(file.uri);
      creating.current = true; setPreparing(true);
      void runtime.imports.createFromSelection(selections).finally(() => { creating.current = false; setPreparing(false); });
    }
  };
  return <><div className="panel-heading"><span className="eyebrow">BRING YOUR HISTORY</span><h1>Every run counts.</h1><p>Import GPX or FIT files. We’ll find the streets you’ve already explored.</p></div>
    <input ref={input} className="file-input" type="file" accept=".gpx,.fit" multiple onChange={(event) => accept(Array.from(event.target.files ?? []))} />
    <button className="dropzone" disabled={preparing || state.uploadingBatchId !== null} onClick={() => choose()}><span className="upload-orb"><Icon name="upload" size={27} /></span><strong>{state.uploadingBatchId ? 'Uploading your activity…' : preparing ? 'Preparing files…' : 'Choose activity files'}</strong><span>GPX or FIT · up to 20 files · 10 MiB each</span></button>
    <p className="note">Files upload one at a time. Accepted activities continue processing even when you stop uploading.</p>
    {state.error && <div className="alert" role="alert">{state.error}</div>}
    {state.rejected.map((item, index) => <div className="alert" key={index}>{item.name}: {item.reason}</div>)}
    <div className="section-title"><h2>Import history</h2><button className="icon-button" title="Refresh import status" aria-label="Refresh import status" onClick={() => void runtime.imports.refresh()} disabled={state.loading}><Icon name="refresh" size={17} /></button></div>
    {state.pollingStopped && <p className="note">Automatic refresh paused. Refresh import status to continue.</p>}
    {!state.batches.length && <div className="empty"><Icon name="route" size={30} /><h3>{state.loading ? 'Loading import history…' : 'Your history starts here'}</h3><p>Completed imports stay here when you return.</p></div>}
    {state.batches.map((batch) => <article className="import-batch" key={batch.id}><div className="batch-heading"><strong>{new Date(batch.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}</strong><span>{batch.items.length} files</span></div>
      <div className="upload-progress"><span>{batch.items.length - batch.counts.awaiting_upload} of {batch.items.length} files uploaded</span><progress aria-label="Files uploaded" max={batch.items.length} value={batch.items.length - batch.counts.awaiting_upload} /></div>
      {state.uploadingBatchId === batch.id && <p className="upload-current" role="status">{batch.state === 'stopped' ? 'Finishing the current request…' : state.uploadingItemId ? `Uploading file ${batch.items.findIndex((item) => item.id === state.uploadingItemId) + 1} of ${batch.items.length}` : 'Preparing upload…'}<strong>{batch.items.find((item) => item.id === state.uploadingItemId)?.name}</strong></p>}
      <div className="batch-summary"><span>{batch.counts.succeeded} imported</span><span>{batch.counts.queued + batch.counts.processing} processing</span><span>{batch.counts.awaiting_upload} waiting</span>{batch.counts.failed > 0 && <span className="error-text">{batch.counts.failed} need attention</span>}</div>
      {batch.items.map((item) => <div className="import-item" key={item.id}><div className={`file-badge ${item.format}`}>{item.format.toUpperCase()}</div><div className="item-copy">{item.status === 'succeeded' && item.activity_id ? <button className="import-activity-link" onClick={() => onOpenActivity(item.activity_id!)} title="Open activity and show route">{item.name}<Icon name="arrow" size={13} /></button> : <strong>{item.name}</strong>}<span className={`status ${item.status}`}>{state.uploadingBatchId === batch.id && state.uploadingItemId === item.id ? 'Uploading…' : item.status === 'awaiting_upload' ? runtime.imports.hasSelectedFile(batch.id, item.id) ? 'Ready to upload' : 'File needed' : item.status === 'succeeded' && item.duplicate ? 'Already imported' : STATUS[item.status]}{item.duplicate && item.status !== 'succeeded' ? ' · same file' : ''}</span>{item.error && <span className="error-text">{FILE_ERRORS.has(item.error) ? 'This file cannot be processed. Choose a corrected file and start a new import.' : 'Processing failed. Retry processing when connected.'}</span>}</div>
        {item.status === 'succeeded' && <Icon name="check" size={17} />}
        {item.status === 'awaiting_upload' && !runtime.imports.hasSelectedFile(batch.id, item.id) && <button className="text-button" disabled={preparing || state.uploadingBatchId !== null} onClick={() => choose({ batchId: batch.id, itemId: item.id })}>Select file</button>}
        {item.status === 'failed' && item.source_id && !FILE_ERRORS.has(item.error ?? '') && <button className="text-button" onClick={() => void runtime.imports.retry(batch.id, item.id)}>Retry processing</button>}
      </div>)}
      {batch.state === 'open' && state.uploadingBatchId === null && batch.items.some((item) => item.status === 'awaiting_upload' && runtime.imports.hasSelectedFile(batch.id, item.id)) && <button className="primary full" disabled={preparing} onClick={() => void runtime.imports.resume(batch.id)}>Retry uploading</button>}
      {batch.counts.awaiting_upload > 0 && <>{batch.state === 'stopped' && <p className="pause-note" role="status">Uploading paused. Accepted files keep processing.</p>}{batch.items.some((item) => item.status === 'awaiting_upload' && !runtime.imports.hasSelectedFile(batch.id, item.id)) && <p className="note">Select each missing file using its listed name.{batch.state === 'stopped' ? ' Then resume uploading.' : ' Selected files upload automatically.'}</p>}<button className="secondary full" onClick={() => void (batch.state === 'open' ? runtime.imports.stop(batch.id) : runtime.imports.resume(batch.id))}>{batch.state === 'open' ? 'Stop uploading' : 'Resume uploading'}</button></>}
    </article>)}
    {state.batches.length < state.total && <button className="secondary full" onClick={() => void runtime.imports.loadMore()} disabled={state.loading}>Load older imports</button>}
  </>;
}
