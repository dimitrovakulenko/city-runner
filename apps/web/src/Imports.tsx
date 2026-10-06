import { useRef } from 'react';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Icon } from './icons';
import { prepareImportSelection } from '../../mobile/src/importSelection';

export function Imports({ runtime }: { runtime: Runtime }) {
  const state = useStore(runtime.imports);
  const input = useRef<HTMLInputElement>(null);
  const target = useRef<{ batchId: string; itemId: string } | null>(null);
  const selectionGeneration = useRef(0);
  const choose = (item?: { batchId: string; itemId: string }) => {
    target.current = item ?? null; selectionGeneration.current = runtime.imports.getAccountGeneration();
    if (input.current) { input.current.multiple = !item; input.current.value = ''; input.current.click(); }
  };
  const accept = (files: File[]) => {
    if (selectionGeneration.current !== runtime.imports.getAccountGeneration()) return;
    if (target.current) {
      const item = target.current; const file = files[0]; if (!file) return;
      const selection = runtime.files.register(file);
      void runtime.imports.reselect(item.batchId, item.itemId, selection).then((accepted) => { if (!accepted) runtime.files.release(selection.uri); });
    } else {
      const selections = files.map((file) => runtime.files.register(file));
      const accepted = new Set(prepareImportSelection(selections).accepted.map((file) => file.uri));
      for (const file of selections) if (!accepted.has(file.uri)) runtime.files.release(file.uri);
      void runtime.imports.createFromSelection(selections);
    }
  };
  return <><div className="panel-heading"><span className="eyebrow">BRING YOUR HISTORY</span><h1>Every run counts.</h1><p>Import GPX or FIT files. We’ll find the streets you’ve already explored.</p></div>
    <input ref={input} className="file-input" type="file" accept=".gpx,.fit" multiple onChange={(event) => accept(Array.from(event.target.files ?? []))} />
    <button className="dropzone" disabled={state.uploadingBatchId !== null} onClick={() => choose()}><span className="upload-orb"><Icon name="upload" size={27} /></span><strong>{state.uploadingBatchId ? 'Uploading your activity…' : 'Choose activity files'}</strong><span>GPX or FIT · up to 20 files · 10 MiB each</span></button>
    <p className="note">Files upload one at a time. Accepted activities continue processing even when you stop uploading.</p>
    {state.error && <div className="alert" role="alert">{state.error}</div>}
    {state.rejected.map((item, index) => <div className="alert" key={index}>{item.name}: {item.reason}</div>)}
    <div className="section-title"><h2>Import history</h2><button className="icon-button" title="Refresh import status" aria-label="Refresh import status" onClick={() => void runtime.imports.refresh()} disabled={state.loading}><Icon name="refresh" size={17} /></button></div>
    {state.pollingStopped && <p className="note">Automatic refresh paused. Refresh import status to continue.</p>}
    {!state.batches.length && <div className="empty"><Icon name="route" size={30} /><h3>{state.loading ? 'Loading import history…' : 'Your history starts here'}</h3><p>Completed imports stay here when you return.</p></div>}
    {state.batches.map((batch) => <article className="import-batch" key={batch.id}><div className="batch-heading"><strong>{new Date(batch.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}</strong><span>{batch.items.length} files</span></div>
      <div className="batch-summary"><span>{batch.counts.succeeded} imported</span><span>{batch.counts.queued + batch.counts.processing} processing</span><span>{batch.counts.awaiting_upload} waiting</span></div>
      {batch.items.map((item) => <div className="import-item" key={item.id}><div className={`file-badge ${item.format}`}>{item.format.toUpperCase()}</div><div className="item-copy"><strong>{item.name}</strong><span className={`status ${item.status}`}>{state.uploadingBatchId === batch.id && state.uploadingItemId === item.id ? 'Uploading…' : item.status.replaceAll('_', ' ')}{item.duplicate ? ' · duplicate' : ''}</span>{item.error && <span className="error-text">{item.error}</span>}</div>
        {item.status === 'succeeded' && <Icon name="check" size={17} />}
        {item.status === 'awaiting_upload' && <button className="text-button" disabled={state.uploadingBatchId !== null || batch.state !== 'open'} onClick={() => choose({ batchId: batch.id, itemId: item.id })}>Select file</button>}
        {item.status === 'failed' && item.source_id && <button className="text-button" onClick={() => void runtime.imports.retry(batch.id, item.id)}>Retry</button>}
      </div>)}
      {batch.counts.awaiting_upload > 0 && <><p className="note">After restarting, select each waiting file again using its listed name.</p><button className="secondary full" onClick={() => void (batch.state === 'open' ? runtime.imports.stop(batch.id) : runtime.imports.resume(batch.id))}>{batch.state === 'open' ? 'Stop uploading' : 'Resume uploading'}</button></>}
    </article>)}
    {state.batches.length < state.total && <button className="secondary full" onClick={() => void runtime.imports.loadMore()} disabled={state.loading}>Load older imports</button>}
  </>;
}
