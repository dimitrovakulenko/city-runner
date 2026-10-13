import { useEffect, useRef, useState } from 'react';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Icon } from './icons';
import { FILE_ERRORS } from './importErrors';

export type HistorySelectionMode = 'files' | 'folder' | 'zip';

export function HistoryImports({ runtime, onReselect, onOpenActivity, selectionBusy }: {
  runtime: Runtime;
  onReselect: (id: string, mode: HistorySelectionMode) => void;
  onOpenActivity: (id: string) => void;
  selectionBusy: boolean;
}) {
  const state = useStore(runtime.imports);
  const [openId, setOpenId] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [mutating, setMutating] = useState<string | null>(null);
  const mutationRequest = useRef(0);
  useEffect(() => { if (openId && !state.histories.some((history) => history.id === openId)) { setOpenId(null); setPage(0); } }, [state.histories, openId]);
  const mutate = (id: string, action: 'stop' | 'resume') => {
    const generation = runtime.imports.getAccountGeneration(); const request = ++mutationRequest.current; setMutating(id);
    void (action === 'stop' ? runtime.imports.stopHistory(id) : runtime.imports.resumeHistory(id)).finally(() => {
      if (generation === runtime.imports.getAccountGeneration() && request === mutationRequest.current) setMutating(null);
    });
  };
  return <>
    {state.historyError && <div className="alert" role="alert">{state.historyError}
      {runtime.imports.hasPendingHistoryCreation() && <button className="text-button" disabled={state.pendingHistoryCreation} onClick={() => void runtime.imports.retryHistoryCreation()}>Retry creating import</button>}
    </div>}
    {state.histories.map((history) => {
      const batches = state.historyBatches[history.id] ?? [];
      const visible = batches[Math.min(page, Math.max(0, batches.length - 1))];
      const current = batches.find((batch) => batch.id === state.uploadingBatchId);
      const uploaded = history.file_count - history.counts.awaiting_upload;
      const busy = mutating === history.id && (history.state === 'stopped' || !current);
      return <article className="import-batch history-import" aria-label={`History import ${history.label}`} key={history.id}>
        <div className="batch-heading"><strong>{history.label}</strong><span>{history.file_count.toLocaleString()} files</span></div>
        <div className="upload-progress"><span>{uploaded.toLocaleString()} of {history.file_count.toLocaleString()} files uploaded</span><progress aria-label={`Files uploaded in ${history.label}`} max={history.file_count} value={uploaded} /></div>
        <div className="batch-summary"><span>{history.counts.succeeded} imported</span><span>{history.counts.queued + history.counts.processing} processing</span><span>{history.counts.awaiting_upload} waiting</span>{history.duplicate_count > 0 && <span>{history.duplicate_count} same files</span>}{history.counts.failed > 0 && <span className="error-text">{history.counts.failed} need attention</span>}{history.counts.deleted > 0 && <span>{history.counts.deleted} deleted</span>}</div>
        {current && <p className="upload-current" role="status">Uploading one file at a time<strong>{current.items.find((item) => item.id === state.uploadingItemId)?.name}</strong></p>}
        {history.counts.awaiting_upload > 0 && <>
          {history.state === 'stopped' && <p className="pause-note" role="status">Uploading paused. Accepted files keep processing.</p>}
          <p className="note">After returning, select the ZIP, folder or files again. Only matching waiting files will upload.</p>
          <div className="history-actions"><button className="secondary" disabled={busy} onClick={() => mutate(history.id, history.state === 'open' ? 'stop' : 'resume')}>{history.state === 'open' ? 'Stop uploading' : 'Resume uploading'}</button>
            <button className="secondary" disabled={selectionBusy || busy || state.uploadingBatchId !== null} onClick={() => onReselect(history.id, 'zip')}>Select ZIP again</button>
            <button className="secondary" disabled={selectionBusy || busy || state.uploadingBatchId !== null} onClick={() => onReselect(history.id, 'folder')}>Select folder again</button>
            <button className="text-button" disabled={selectionBusy || busy || state.uploadingBatchId !== null} onClick={() => onReselect(history.id, 'files')}>Select files again</button>
          </div>
          {history.state === 'open' && !state.uploadingBatchId && batches.some((batch) => batch.items.some((item) => item.status === 'awaiting_upload' && runtime.imports.hasSelectedFile(batch.id, item.id))) && <button className="primary full" disabled={busy} onClick={() => mutate(history.id, 'resume')}>Retry uploading</button>}
        </>}
        <button className="text-button" disabled={state.historyBatchesLoading[history.id]} onClick={() => {
          if (openId === history.id) { setOpenId(null); return; }
          setOpenId(history.id); setPage(0); void runtime.imports.loadHistoryBatches(history.id);
        }}>{openId === history.id ? 'Hide files' : 'Show files'}</button>
        {openId === history.id && <div className="history-files">
          {state.historyBatchesLoading[history.id] && <p className="note" role="status">Loading files…</p>}
          {state.historyBatchesErrors[history.id] && <div className="alert" role="alert">{state.historyBatchesErrors[history.id]}<button className="text-button" onClick={() => void runtime.imports.loadHistoryBatches(history.id)}>Retry loading files</button></div>}
          {visible?.items.map((item) => <div className="import-item" key={item.id}><div className={`file-badge ${item.format}`}>{item.format.toUpperCase()}</div><div className="item-copy">
            {item.status === 'succeeded' && item.activity_id ? <button className="import-activity-link" onClick={() => onOpenActivity(item.activity_id!)}>{item.name}<Icon name="arrow" size={13} /></button> : <strong>{item.name}</strong>}
            <span className={`status ${item.status}`}>{state.uploadingItemId === item.id ? 'Uploading…' : item.status === 'awaiting_upload' ? runtime.imports.hasSelectedFile(visible.id, item.id) ? 'Ready to upload' : 'File needed' : item.status === 'succeeded' && item.duplicate ? 'Already imported' : item.status === 'succeeded' ? 'Imported' : item.status === 'failed' ? 'Needs attention' : item.status === 'queued' ? 'Waiting to process' : item.status === 'processing' ? 'Processing activity' : 'Deleted'}</span>
            {item.error && <span className="error-text">{FILE_ERRORS.has(item.error) ? 'Choose a corrected file and start a new import.' : 'Processing failed. Retry when connected.'}</span>}
          </div>{item.status === 'succeeded' && <Icon name="check" size={17} />}{item.status === 'failed' && item.source_id && !FILE_ERRORS.has(item.error ?? '') && <button className="text-button" onClick={() => void runtime.imports.retry(visible.id, item.id)}>Retry processing</button>}</div>)}
          {batches.length > 1 && <div className="history-actions"><button className="text-button" disabled={page === 0} onClick={() => setPage((value) => value - 1)}>Previous files</button><span className="note">Files {Math.min(page * 20 + 1, history.file_count)}–{Math.min((page + 1) * 20, history.file_count)} of {history.file_count}</span><button className="text-button" disabled={page >= batches.length - 1} onClick={() => setPage((value) => value + 1)}>Next files</button></div>}
        </div>}
      </article>;
    })}
    {state.histories.length < state.historyTotal && <button className="secondary full" disabled={state.historiesLoading} onClick={() => void runtime.imports.loadMoreHistories()}>Load older history imports</button>}
  </>;
}
