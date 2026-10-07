import { useState } from 'react';
import type { Runtime } from './runtime';
import { useStore } from './hooks';

export function Account({ runtime, accountId, onDeleted }: { runtime: Runtime; accountId: string; onDeleted: (accountId: string, status: 'cleanup-pending' | 'complete') => void }) {
  const state = useStore(runtime.account);
  const [confirming, setConfirming] = useState(false); const [confirmation, setConfirmation] = useState('');
  const busy = state.exportStatus === 'loading' || state.deleteStatus === 'loading';
  const download = async () => {
    const generation = runtime.account.getAccountGeneration();
    const result = await runtime.account.exportAccount();
    if (!result || generation !== runtime.account.getAccountGeneration()) return;
    const url = URL.createObjectURL(result.blob);
    try { const link = document.createElement('a'); link.href = url; link.download = result.fileName; document.body.append(link); link.click(); link.remove(); }
    finally { setTimeout(() => URL.revokeObjectURL(url), 1000); }
  };
  const remove = async () => {
    const receipt = await runtime.account.deleteAccount(confirmation);
    if (receipt) onDeleted(accountId, receipt.status);
  };
  return <section aria-label="Account data">
    <div className="panel-heading"><span className="eyebrow">YOUR ACCOUNT</span><h1>Your data</h1><p>Download your history or permanently remove your account.</p></div>
    <div className="detail-card"><h2>Export your data</h2><p className="note">A ZIP archive contains your original GPX/FIT files, activity tracks and timestamps, source/import history, GPS contributions, manual labels and saved routes.</p><button className="secondary full" disabled={busy || !navigator.onLine} onClick={() => void download()}>{state.exportStatus === 'loading' ? 'Preparing archive…' : 'Download account archive'}</button>{state.exportError && <p className="alert" role="alert">{state.exportError}</p>}{state.exportStatus === 'ready' && <p className="note" role="status">Your archive is ready. Check your browser downloads.</p>}</div>
    <div className="detail-card account-delete"><h2>Delete account</h2><p className="note">Permanently removes your activities, coverage, imports, saved routes and manual labels. All sessions are revoked. Original files are removed after deletion.</p>{confirming ? <form onSubmit={(event) => { event.preventDefault(); void remove(); }}><label className="field-label">Type DELETE to confirm<input aria-label="Account deletion confirmation" value={confirmation} autoComplete="off" onChange={(event) => setConfirmation(event.target.value)} disabled={busy} /></label><p className="note">Deletion cannot be undone. Export your archive first if you want a copy.</p><div className="filter-actions"><button className="primary destructive" type="submit" disabled={busy || confirmation !== 'DELETE' || !navigator.onLine}>{state.deleteStatus === 'loading' ? 'Deleting account…' : 'Permanently delete account'}</button><button className="text-button" type="button" disabled={busy} onClick={() => { setConfirming(false); setConfirmation(''); }}>Keep account</button></div></form> : <button className="danger text-button" disabled={busy} onClick={() => setConfirming(true)}>Delete my account</button>}{state.deleteError && <p className="alert" role="alert">{state.deleteError}</p>}</div>
  </section>;
}
