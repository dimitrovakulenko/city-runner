import { useEffect, useState } from 'react';
import type { AnalyticsHandoffRequest, AnalyticsStatus, AnalyticsTemplateRequest } from '../../mobile/src/api/generated';
import type { Runtime } from './runtime';
import { useStore } from './hooks';

export function Insights({ runtime }: { runtime: Runtime }) {
  const cities = useStore(runtime.cities);
  const generation = runtime.activities.getAccountGeneration();
  const [view, setView] = useState<{ generation: number; data: AnalyticsStatus } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  const state = view?.generation === generation ? view.data : null;
  useEffect(() => {
    const controller = new AbortController();
    let loading = false;
    setView(null); setError(null); setBusy(false);
    const load = async () => {
      if (loading || document.visibilityState !== 'visible') return;
      loading = true;
      try {
        const data = await runtime.analytics.status(controller.signal);
        if (!controller.signal.aborted && generation === runtime.activities.getAccountGeneration()) { setView({ generation, data }); setError(null); }
      } catch (failure) {
        if (!controller.signal.aborted && generation === runtime.activities.getAccountGeneration()) setError(failure instanceof Error ? failure.message : 'Insights could not load.');
      } finally { loading = false; }
    };
    void load();
    const timer = setInterval(() => { void load(); }, 5000);
    document.addEventListener('visibilitychange', load);
    return () => { controller.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', load); };
  }, [runtime, generation, retry]);

  const change = async (body: Parameters<Runtime['analytics']['preferences']>[0]) => {
    setBusy(true); setError(null);
    try {
      await runtime.analytics.preferences(body);
      const data = await runtime.analytics.status();
      if (generation === runtime.activities.getAccountGeneration()) setView({ generation, data });
    } catch (failure) { if (generation === runtime.activities.getAccountGeneration()) setError(failure instanceof Error ? failure.message : 'Preference could not be saved.'); }
    finally { if (generation === runtime.activities.getAccountGeneration()) setBusy(false); }
  };

  const open = async (destination: AnalyticsHandoffRequest, template?: AnalyticsTemplateRequest['template']) => {
    const popup = window.open('about:blank', '_blank');
    if (!popup) { setError('Allow a new tab to open Insights.'); return; }
    popup.opener = null;
    let submitted = false;
    setBusy(true); setError(null);
    try {
      if (template) {
        const board = await runtime.analytics.copy({ template });
        destination = { destination: 'dashboard', dashboard_id: board.id };
      }
      const handoff = await runtime.analytics.handoff(destination);
      if (generation !== runtime.activities.getAccountGeneration()) { popup.close(); return; }
      const form = popup.document.createElement('form');
      form.method = 'POST'; form.action = handoff.action;
      const ticket = popup.document.createElement('input');
      ticket.type = 'hidden'; ticket.name = 'ticket'; ticket.value = handoff.ticket;
      form.append(ticket); popup.document.body.append(form); form.submit();
      submitted = true;
      const data = await runtime.analytics.status();
      if (generation === runtime.activities.getAccountGeneration()) setView({ generation, data });
    } catch (failure) {
      if (!submitted) popup.close();
      if (generation === runtime.activities.getAccountGeneration()) setError(failure instanceof Error ? failure.message : 'Insights could not open.');
    } finally { if (generation === runtime.activities.getAccountGeneration()) setBusy(false); }
  };

  const ready = state?.state === 'ready' && !error;
  const boards = state?.boards ?? [];
  return <section aria-label="Insights">
    <div className="panel-heading"><span className="eyebrow">YOUR DATA, YOUR QUESTIONS</span><h1>Insights</h1><p>Create charts, write SQL, and build private boards from your GPX and FIT history.</p></div>
    {!state && !error && <p role="status">Loading Insights…</p>}
    {state?.state === 'disabled' ? <p className="note">Insights is not configured on this server.</p> : state && <>
      <label className="field-label">Insights region<select aria-label="Insights region" value={state.dataset_id ?? ''} disabled={busy} onChange={(event) => void change({ dataset_id: event.target.value || null })}>
        <option value="">Activities only</option>
        {state.dataset_id && !cities.datasets.some((dataset) => dataset.dataset_id === state.dataset_id) && <option value={state.dataset_id}>Selected region</option>}
        {cities.datasets.map((dataset) => <option key={dataset.dataset_id} value={dataset.dataset_id}>{dataset.region}</option>)}
      </select></label>
      {!ready && <p className="note" role="status">{state.state === 'failed' ? 'Insights could not refresh. It will retry automatically.' : 'Your data is refreshing. Boards will be available when the snapshot is current.'}</p>}
      <p className="note">Each board has its own filters. Starter coverage uses the normal rule; manual completion is shown separately. Unknown values stay blank. Dates contain no time of day.</p>
      <button className="primary full" disabled={!ready || busy} onClick={() => void open({ destination: 'default' })}>Open Insights</button>
      <div className="modal-actions"><button className="secondary" disabled={!ready || busy} onClick={() => void open({ destination: 'sql' })}>SQL Lab</button><button className="secondary" disabled={!ready || busy} onClick={() => void open({ destination: 'boards' })}>All boards</button><button className="secondary" disabled={!ready || busy} onClick={() => void open({}, 'blank')}>New board</button></div>
      <p className="note">The full chart and board editor opens in a separate tab. Changes save to your account.</p>
      <label className="field-label">Default board<select aria-label="Default board" value={state.default_dashboard_id ?? ''} disabled={!ready || busy} onChange={(event) => void change({ default_dashboard_id: event.target.value || null })}>
        <option value="">Board list</option>{boards.map((board) => <option key={board.id} value={board.id}>{board.title}</option>)}
      </select></label>
      <div className="section-title"><h2>Your boards</h2></div>
      {ready && !boards.length && <p className="note">No boards yet. Start with a template or create a blank board.</p>}
      <div className="activity-list">{boards.map((board) => <button className="activity-row" key={board.id} disabled={!ready || busy} onClick={() => void open({ destination: 'dashboard', dashboard_id: board.id })}><span className="row-copy"><strong>{board.title}</strong><span>Open and edit board</span></span></button>)}</div>
      <div className="section-title"><h2>Start from a template</h2></div>
      <p className="note">Each copy is yours to edit. Template updates leave your saved work intact.</p>
      {([['activity', 'Activity'], ['exploration', 'Exploration'], ['next-streets', 'Next streets']] as const).map(([template, title]) => <button className="secondary full" key={template} disabled={!ready || busy} onClick={() => void open({}, template)}>Copy {title} template</button>)}
      <details className="recording-details"><summary>SQL examples and data notes</summary><p className="note">Distances are metres, durations seconds, pace seconds per kilometre. Geography metrics require a ready selected region. History is recomputed from retained activities; same-day ordering is approximate.</p>
        <pre className="insights-sql">{`SELECT activity_type, count(*) AS activities,\n  sum(distance_m) / 1000 AS known_km,\n  count(*) FILTER (WHERE distance_m IS NULL) AS unknown\nFROM analytics.activities\nGROUP BY activity_type;`}</pre>
        <p className="note">For an arbitrary period, count distinct supported tokens. Summing daily new nodes answers a different question.</p>
        <pre className="insights-sql">{`SELECT CASE WHEN (SELECT coverage_status = 'ready'\n  FROM analytics.dataset_status) THEN count(DISTINCT node_token)\n  ELSE NULL END AS supported_nodes\nFROM analytics.activity_nodes\nWHERE recorded_date >= '2026-01-01'\n  AND recorded_date < '2027-01-01';`}</pre>
      </details>
    </>}
    {error && <div className="alert" role="alert">{error}<button className="text-button" onClick={() => setRetry((value) => value + 1)}>Retry Insights</button></div>}
  </section>;
}
