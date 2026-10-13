import { useRef, useState } from 'react';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Icon } from './icons';
import { prepareImportSelection } from '../../mobile/src/importSelection';
import { historyBasename } from '../../mobile/src/historyImportSelection';
import { prepareBrowserHistory, type PreparationProgress } from './historyFiles';
import { HistoryImports, type HistorySelectionMode } from './HistoryImports';
import { FILE_ERRORS } from './importErrors';

export { FILE_ERRORS } from './importErrors';
const STATUS = { queued: 'Waiting to process', processing: 'Processing activity', succeeded: 'Imported', failed: 'Needs attention', deleted: 'Deleted' };

export function Imports({ runtime, onOpenActivity }: { runtime: Runtime; onOpenActivity: (id: string) => void }) {
  const state = useStore(runtime.imports);
  const input = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null); const zipInput = useRef<HTMLInputElement>(null);
  const target = useRef<{ batchId: string; itemId: string } | { historyId: string } | null>(null);
  const selectionMode = useRef<HistorySelectionMode>('files');
  const selectionGeneration = useRef(0);
  const creating = useRef(false); const [preparing, setPreparing] = useState(false);
  const [progress, setProgress] = useState<PreparationProgress | null>(null);
  const preparation = useRef<AbortController | null>(null);
  const [skipped, setSkipped] = useState(0);
  const selectionBusy = preparing || runtime.files.isPreparing() || state.pendingHistoryCreation || runtime.imports.hasPendingHistoryCreation();
  const choose = (item?: { batchId: string; itemId: string } | { historyId: string }, mode: HistorySelectionMode = 'files') => {
    if (creating.current || runtime.files.isPreparing() || runtime.imports.getState().pendingHistoryCreation || runtime.imports.hasPendingHistoryCreation()) return;
    target.current = item ?? null; selectionGeneration.current = runtime.imports.getAccountGeneration();
    selectionMode.current = mode;
    const picker = mode === 'folder' ? folderInput.current : mode === 'zip' ? zipInput.current : input.current;
    if (picker) { picker.multiple = mode !== 'zip' && !(item && 'itemId' in item); picker.value = ''; picker.click(); }
  };
  const accept = async (files: File[]) => {
    const generation = selectionGeneration.current;
    if (!files.length || runtime.files.isPreparing() || runtime.imports.getState().pendingHistoryCreation || runtime.imports.hasPendingHistoryCreation() || generation !== runtime.imports.getAccountGeneration()) return;
    if (target.current && 'itemId' in target.current) {
      const item = target.current; const file = files[0]; if (!file) return;
      const selection = runtime.files.register(file);
      await runtime.imports.reselect(item.batchId, item.itemId, selection).then((accepted) => { if (!accepted) runtime.files.release(selection.uri); });
    } else {
      const selectedTarget = target.current; const mode = selectionMode.current;
      if (!selectedTarget && mode === 'files' && files.length <= 20 && !files.some((file) => file.name.toLowerCase().endsWith('.zip'))) {
        const selections = files.map((file) => runtime.files.register(file));
        const accepted = new Set(prepareImportSelection(selections).accepted.map((file) => file.uri));
        for (const file of selections) if (!accepted.has(file.uri)) runtime.files.release(file.uri);
        creating.current = true; setPreparing(true);
        try { await runtime.imports.createFromSelection(selections); }
        finally {
          for (const file of selections) if (!runtime.imports.retainsFile(file.uri)) runtime.files.release(file.uri);
          if (generation === runtime.imports.getAccountGeneration()) { creating.current = false; setPreparing(false); }
        }
        return;
      }
      const controller = runtime.files.beginPreparation(); preparation.current = controller;
      const registered: string[] = [];
      runtime.imports.setError(''); setPreparing(true); setProgress(null); setSkipped(0);
      try {
        if (selectedTarget || mode !== 'files' || files.length > 20 || files.some((file) => file.name.toLowerCase().endsWith('.zip'))) {
          const selected = await prepareBrowserHistory(files, controller.signal, (value) => { if (generation === runtime.imports.getAccountGeneration()) setProgress(value); }, mode === 'folder');
          if (controller.signal.aborted || generation !== runtime.imports.getAccountGeneration()) return;
          const selections = selected.files.map(({ file, kind, sha256, size }) => {
            const local = runtime.files.register(file); registered.push(local.uri); return { ...local, kind, sha256, size };
          });
          setPreparing(false); setSkipped(selected.skipped);
          if (selectedTarget && 'historyId' in selectedTarget) await runtime.imports.reselectHistory(selectedTarget.historyId, selections);
          else {
            const rawLabel = mode === 'folder' ? files[0].webkitRelativePath?.split('/')[0] || 'Activity history' : files.length === 1 ? files[0].name : 'Activity history';
            const label = Array.from(historyBasename(rawLabel)).slice(0, 200).join('') || 'Activity history';
            await runtime.imports.createHistoryFromSelection(label, selections);
          }
        }
      } catch (error) {
        if (generation === runtime.imports.getAccountGeneration() && !controller.signal.aborted) runtime.imports.setError(error instanceof Error ? error.message : 'Could not read these files. Select them again.');
      } finally {
        for (const uri of registered) if (generation !== runtime.imports.getAccountGeneration() || !runtime.imports.retainsFile(uri)) runtime.files.release(uri);
        runtime.files.finishPreparation(controller);
        if (preparation.current === controller) preparation.current = null;
        if (generation === runtime.imports.getAccountGeneration()) { setPreparing(false); setProgress(null); }
      }
    }
  };
  return <><div className="panel-heading"><span className="eyebrow">BRING YOUR HISTORY</span><h1>Every run counts.</h1><p>Import GPX or FIT files. We’ll find the streets you’ve already explored.</p></div>
    <input ref={input} aria-label="Choose activity files" className="file-input" type="file" accept=".gpx,.fit" multiple onChange={(event) => { const files = Array.from(event.target.files ?? []); event.target.value = ''; void accept(files); }} />
    <input ref={folderInput} aria-label="Choose activity folder" className="file-input" type="file" {...{ webkitdirectory: '', directory: '' }} multiple onChange={(event) => { const files = Array.from(event.target.files ?? []); event.target.value = ''; void accept(files); }} />
    <input ref={zipInput} aria-label="Choose history ZIP" className="file-input" type="file" accept=".zip" onChange={(event) => { const files = Array.from(event.target.files ?? []); event.target.value = ''; void accept(files); }} />
    <button className="dropzone" disabled={selectionBusy || state.uploadingBatchId !== null} onClick={() => choose()}><span className="upload-orb"><Icon name="upload" size={27} /></span><strong>{state.uploadingBatchId ? 'Uploading your activity…' : selectionBusy ? 'Preparing files…' : 'Choose activity files'}</strong><span>GPX or FIT · up to 1,000 files · 10 MiB each</span></button>
    <div className="history-actions"><button className="secondary" disabled={selectionBusy || state.uploadingBatchId !== null} onClick={() => choose(undefined, 'folder')}>Choose folder</button><button className="secondary" disabled={selectionBusy || state.uploadingBatchId !== null} onClick={() => choose(undefined, 'zip')}>Import ZIP</button></div>
    {preparing && <p className="note" role="status">{progress ? `Preparing file ${Math.min(progress.current + 1, progress.total)} of ${progress.total}…` : 'Checking and preparing files…'} <button className="text-button" onClick={() => preparation.current?.abort()}>Cancel preparation</button></p>}
    {skipped > 0 && <p className="note">Skipped {skipped} non-activity {skipped === 1 ? 'file' : 'files'}.</p>}
    <p className="note">ZIPs up to 64 MiB. Up to 256 MiB of activity files per history import.</p>
    <p className="note">Files upload one at a time. Accepted activities continue processing even when you stop uploading.</p>
    {state.error && <div className="alert" role="alert">{state.error}</div>}
    {state.rejected.map((item, index) => <div className="alert" key={index}>{item.name}: {item.reason}</div>)}
    <div className="section-title"><h2>Import history</h2><button className="icon-button" title="Refresh import status" aria-label="Refresh import status" onClick={() => void runtime.imports.refresh()} disabled={state.loading}><Icon name="refresh" size={17} /></button></div>
    {state.pollingStopped && <p className="note">Automatic refresh paused. Refresh import status to continue.</p>}
    <HistoryImports runtime={runtime} onOpenActivity={onOpenActivity} selectionBusy={selectionBusy} onReselect={(id, mode) => choose({ historyId: id }, mode)} />
    {!state.batches.length && !state.histories.length && <div className="empty"><Icon name="route" size={30} /><h3>{state.loading || state.historiesLoading ? 'Loading import history…' : 'Your history starts here'}</h3><p>Completed imports stay here when you return.</p></div>}
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
