import { useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import type { CityExplorerStore, CityExplorerState, CityLoadState } from './cityExplorerStore';
import type { RemainingNode } from './api/generated';
import type { StreetFilter } from './api/cities';
import type { CorrectionState, CorrectionStore } from './correctionStore';

const FILTERS: StreetFilter[] = ['all', 'incomplete', 'partial', 'completed'];

export function CityExplorerScreen({ store, correctionStore, enabled, fixtureMode, accountGeneration, onShowNode, onOpenActivity, onMarkComplete, onUndoManual }: {
  store: CityExplorerStore;
  correctionStore: CorrectionStore;
  enabled: boolean;
  fixtureMode: boolean;
  accountGeneration: number;
  onShowNode: (node: RemainingNode, rule: 'normal' | 'strict', generation: number) => void;
  onOpenActivity: (id: string, generation: number) => void;
  onMarkComplete: (streetId: string, datasetId: string, reason: string, generation: number) => void;
  onUndoManual: (streetId: string, datasetId: string, generation: number) => void;
}) {
  const [state, setState] = useState<CityExplorerState>(store.getState());
  const [correction, setCorrection] = useState<CorrectionState>(correctionStore.getState());
  const [manualReason, setManualReason] = useState('');
  const [manualConfirmation, setManualConfirmation] = useState(false);
  useEffect(() => {
    const unsubscribe = store.subscribe(setState);
    if (enabled && !fixtureMode) void store.refreshProgress();
    return unsubscribe;
  }, [accountGeneration, enabled, fixtureMode, store]);
  useEffect(() => correctionStore.subscribe(setCorrection), [correctionStore]);
  useEffect(() => {
    setManualReason(''); setManualConfirmation(false); correctionStore.clearFeedback();
  }, [accountGeneration, state.selectedDatasetId, state.selectedCityId, state.selectedStreetId, state.rule, correctionStore]);

  const citySelected = state.selectedCityId !== null;
  const streetSelected = state.selectedStreetId !== null;
  const title = streetSelected ? state.detail?.name ?? 'Street details' : citySelected ? state.cities.find((city) => city.id === state.selectedCityId)?.name ?? 'Streets' : 'Cities';
  return <View style={styles.shell}>
    <View style={styles.header}>
      <View style={{ flex: 1 }}><Text style={styles.kicker}>CITY EXPLORER</Text><Text style={styles.title}>{title}</Text></View>
      <Pressable disabled={!enabled || fixtureMode} onPress={() => void store.retry()} style={[styles.button, (!enabled || fixtureMode) && styles.disabled]}><Text style={styles.buttonText}>↻ Retry</Text></Pressable>
    </View>
    <View style={styles.rules}>{(['normal', 'strict'] as const).map((rule) => <Pressable key={rule} disabled={!enabled || fixtureMode} onPress={() => store.setRule(rule)} style={[styles.rule, rule === state.rule && styles.ruleSelected, (!enabled || fixtureMode) && styles.disabled]}>
      <Text style={[styles.ruleText, rule === state.rule && styles.ruleTextSelected]}>{rule === 'normal' ? 'Normal · 90%' : 'Strict · all nodes'}</Text>
    </Pressable>)}</View>
    {!fixtureMode && !enabled && <Message text="Sign in to browse private city and street progress." />}
    {fixtureMode && <Message text="City and street coverage is unavailable in fixture mode." />}
    {enabled && !fixtureMode && <ScrollView style={styles.content} keyboardShouldPersistTaps="handled">
      <Text style={styles.label}>ACTIVE DATASET · CHOOSE ONE</Text>
      {state.progressStatus === 'loading' && !state.progress && <Message text="Loading active datasets…" loading />}
      <StateError state={state.progressStatus} error={state.progressError} onRetry={() => void store.refreshProgress()} />
      {state.progress?.datasets_truncated && <Text style={styles.notice}>The active dataset list is limited. Some datasets are not shown.</Text>}
      {state.datasets.map((dataset) => <Pressable key={dataset.dataset_id} onPress={() => store.selectDataset(dataset.dataset_id)} style={[styles.dataset, state.selectedDatasetId === dataset.dataset_id && styles.selected]}>
        <Text style={styles.rowTitle}>{dataset.region}</Text><Text style={styles.copy}>Dataset {dataset.dataset_id} · {dataset.state}</Text>
        {dataset.completed_streets !== null && dataset.eligible_streets !== null && <Text style={styles.copy}>GPS complete: {dataset.completed_streets} / {dataset.eligible_streets}</Text>}
        {dataset.effective_completed_streets !== null && <Text style={styles.copy}>Effective complete: {dataset.effective_completed_streets}</Text>}
        <Text style={styles.copy}>Manual labels: {dataset.manual_completed_streets}</Text>
      </Pressable>)}
      {state.progressStatus === 'ready' && state.datasets.length === 0 && <Message text="No active city datasets are available yet." />}
      {!!state.selectedDatasetId && <>
        {!citySelected && <>
          <TextInput value={state.cityQuery} onChangeText={(value) => store.setCityQuery(value)} placeholder="Search cities" style={styles.search} accessibilityLabel="Search cities" />
          {coverageMessage(state.cityCoverage?.status)}
          <StateError state={state.cityStatus} error={state.cityError} onRetry={() => void store.retry()} />
          {state.cityStatus === 'loading' && <Message text="Loading cities…" loading />}
          {state.cityStatus === 'empty' && <Message text="No cities match this search." />}
          {state.cities.map((city) => <Pressable key={city.id} onPress={() => store.selectCity(city.id)} style={styles.row}>
            <Text style={styles.rowTitle}>{city.name}</Text>
            <Text style={styles.copy}>{cityProgress(city, state.cityCoverage?.status)}</Text>
          </Pressable>)}
          <More state={state.cityStatus} loaded={state.cities.length} total={state.cityTotal} onPress={() => void store.loadMoreCities()} />
        </>}
        {citySelected && !streetSelected && <>
          <Pressable onPress={() => store.backToCities()}><Text style={styles.link}>← Cities</Text></Pressable>
          {coverageMessage(state.streetCoverage?.status)}
          {!state.streetFilterApplied && <Text style={styles.notice}>Coverage is pending. The server cannot apply this filter yet and is showing all matching streets.</Text>}
          <TextInput value={state.streetQuery} onChangeText={(value) => store.setStreetQuery(value)} placeholder="Search streets" style={styles.search} accessibilityLabel="Search streets" />
          <View style={styles.filters}>{FILTERS.map((filter) => <Pressable key={filter} onPress={() => store.setStreetFilter(filter)} style={[styles.filter, state.streetFilter === filter && styles.filterSelected]}><Text style={[styles.filterText, state.streetFilter === filter && styles.filterTextSelected]}>{filter}</Text></Pressable>)}</View>
          <StateError state={state.streetStatus} error={state.streetError} onRetry={() => void store.retry()} />
          {state.streetStatus === 'loading' && <Message text="Loading streets…" loading />}
          {state.streetStatus === 'empty' && <Message text="No streets match this filter." />}
          {state.streets.map((street) => <Pressable key={street.id} onPress={() => store.selectStreet(street.id)} style={styles.row}>
            <Text style={styles.rowTitle}>{street.name}</Text><Text style={styles.copy}>{streetProgress(street, state.streetCoverage?.status)}</Text>
          </Pressable>)}
          <More state={state.streetStatus} loaded={state.streets.length} total={state.streetTotal} onPress={() => void store.loadMoreStreets()} />
        </>}
        {streetSelected && <>
          <Pressable onPress={() => store.backToStreets()}><Text style={styles.link}>← Streets</Text></Pressable>
          {state.detail && <>
            {coverageMessage(state.detail.coverage.status)}
            <Text style={styles.copy}>GPS status: {state.detail.state ?? (state.detail.coverage.status === 'ready' ? 'not available' : `coverage ${state.detail.coverage.status}`)}</Text>
            <Text style={styles.copy}>Effective status: {state.detail.effective_state ?? (state.detail.coverage.status === 'ready' ? 'not available' : 'pending')}</Text>
            {state.detail.manual_completed && <Text style={styles.manualLabel}>MANUALLY COMPLETED</Text>}
            {state.detail.manual_reason && <Text style={styles.copy}>Manual reason: {state.detail.manual_reason}</Text>}
            <Text style={styles.copy}>Manual completion does not change recorded GPS visits or remaining nodes.</Text>
            <Text style={styles.copy}>{state.detail.visited_nodes === null || state.detail.eligible_nodes === null ? 'Node progress pending.' : `${state.detail.visited_nodes} / ${state.detail.eligible_nodes} nodes visited`}</Text>
            {enabled && !fixtureMode && !state.detail.manual_completed && !manualConfirmation && <Pressable disabled={correction.pending !== null} onPress={() => { correctionStore.clearFeedback(); setManualConfirmation(true); setManualReason(''); }} style={styles.smallButton}><Text style={styles.buttonText}>Mark manually complete</Text></Pressable>}
            {enabled && !fixtureMode && !state.detail.manual_completed && manualConfirmation && <View style={styles.manualBox}>
              <Text style={styles.copy}>Add a short reason (1–500 characters).</Text>
              <TextInput value={manualReason} onChangeText={setManualReason} maxLength={500} editable={correction.pending === null} placeholder="Reason" style={styles.search} accessibilityLabel="Manual completion reason" />
              <View style={styles.manualActions}>
                <Pressable disabled={correction.pending !== null} onPress={() => { setManualConfirmation(false); setManualReason(''); correctionStore.clearFeedback(); }}><Text style={styles.link}>Cancel</Text></Pressable>
                <Pressable disabled={correction.pending !== null || !manualReason.trim() || manualReason.trim().length > 500} onPress={() => onMarkComplete(state.detail!.id, state.detail!.dataset_id, manualReason, accountGeneration)}><Text style={styles.buttonText}>{correction.pending === 'manual-complete' ? 'Saving…' : 'Confirm'}</Text></Pressable>
              </View>
            </View>}
            {enabled && !fixtureMode && state.detail.manual_completed && <Pressable disabled={correction.pending !== null} onPress={() => onUndoManual(state.detail!.id, state.detail!.dataset_id, accountGeneration)} style={styles.smallButton}><Text style={styles.buttonText}>{correction.pending === 'manual-undo' ? 'Undoing…' : 'Undo manual completion'}</Text></Pressable>}
            {correction.error && correction.pending === null && <Text accessibilityRole="alert" style={styles.error}>{correction.error}</Text>}
            {correction.message && <Text accessibilityRole="alert" style={styles.manualLabel}>{correction.message}</Text>}
            {state.detail.coverage.status === 'ready' && state.remainingNodes !== null && state.remainingNodes.length === 0 && <Message text="No remaining nodes." />}
            {state.detail.coverage.status !== 'ready' && <Message text="Remaining nodes are withheld until coverage is ready." />}
            {state.remainingNodes?.map((node) => <View key={node.id} style={styles.node}>
              <View style={{ flex: 1 }}><Text style={styles.rowTitle}>Node {node.id}</Text><Text style={styles.copy}>{node.latitude.toFixed(6)}, {node.longitude.toFixed(6)}</Text></View>
              <Pressable onPress={() => onShowNode(node, state.rule, accountGeneration)} style={styles.smallButton}><Text style={styles.buttonText}>Show on map</Text></Pressable>
            </View>)}
            <More state={state.detailStatus} loaded={state.remainingNodes?.length ?? 0} total={state.detail.remaining_nodes_page.total ?? 0} onPress={() => void store.loadMoreNodes()} />
          </>}
          <StateError state={state.detailStatus} error={state.detailError} onRetry={() => void store.retry()} />
          {state.detailStatus === 'loading' && <Message text="Loading street details…" loading />}
          <View style={styles.section}>
            <Text style={styles.label}>CONTRIBUTING ACTIVITIES</Text>
            {coverageMessage(state.contributionCoverage?.status)}
            <StateError state={state.contributionStatus} error={state.contributionError} onRetry={() => void store.retry()} />
            {state.contributionStatus === 'loading' && <Message text="Loading contributions…" loading />}
            {state.contributionCoverage?.status !== 'ready' && state.contributionCoverage && <Message text={`Activity attribution is unavailable while coverage is ${state.contributionCoverage.status}.`} />}
            {state.contributionCoverage?.status === 'ready' && state.contributionAvailable && state.contributions.length === 0 && <Message text="No recorded activities contributed to this street." />}
            {state.contributions.map((activity) => <View key={activity.id} style={styles.contribution}>
              <View style={{ flex: 1 }}><Text style={styles.rowTitle}>{activity.name}</Text><Text style={styles.copy}>{activity.date} · {activity.type} · {activity.supported_nodes} nodes</Text></View>
              <Pressable onPress={() => onOpenActivity(activity.id, accountGeneration)} style={styles.smallButton}><Text style={styles.buttonText}>Open</Text></Pressable>
            </View>)}
            <More state={state.contributionStatus} loaded={state.contributions.length} total={state.contributionTotal ?? 0} onPress={() => void store.loadMoreContributions()} />
          </View>
        </>}
      </>}
    </ScrollView>}
  </View>;
}

function coverageMessage(status: 'ready' | 'pending' | 'failed' | undefined) {
  if (!status || status === 'ready') return null;
  return <Text style={styles.notice}>{status === 'pending' ? 'Coverage matching is still pending. Counts and missing nodes are not available yet.' : 'Coverage matching failed. Retry later; GPS counts are unavailable.'}</Text>;
}
function cityProgress(city: { visited_nodes: number | null; eligible_nodes: number | null; completed_streets: number | null; eligible_streets: number | null; effective_completed_streets: number | null; manual_completed_streets: number }, coverage?: 'ready' | 'pending' | 'failed'): string {
  const gps = city.visited_nodes === null || city.eligible_nodes === null || city.completed_streets === null || city.eligible_streets === null
    ? coverage === 'failed' ? 'GPS unavailable' : 'GPS pending'
    : `${city.visited_nodes} / ${city.eligible_nodes} nodes · GPS ${city.completed_streets} / ${city.eligible_streets}`;
  return `${gps} · effective ${city.effective_completed_streets ?? 'pending'} / ${city.eligible_streets ?? 'pending'} · manual ${city.manual_completed_streets}`;
}
function streetProgress(street: { state: 'complete' | 'partial' | 'missing' | null; visited_nodes: number | null; eligible_nodes: number | null; effective_state: 'complete' | 'partial' | 'missing' | null; manual_completed: boolean }, coverage?: 'ready' | 'pending' | 'failed'): string {
  const gps = street.state === null || street.visited_nodes === null || street.eligible_nodes === null
    ? coverage === 'failed' ? 'GPS unavailable' : 'GPS pending'
    : `GPS ${street.state} · ${street.visited_nodes} / ${street.eligible_nodes} nodes`;
  return `${gps} · effective ${street.effective_state ?? 'pending'} · manual ${street.manual_completed ? 'yes' : 'no'}`;
}
function Message({ text, loading }: { text: string; loading?: boolean }) {
  return <View style={styles.message}>{loading && <ActivityIndicator color="#ef704f" />}<Text style={styles.copy}>{text}</Text></View>;
}
function StateError({ state, error, onRetry }: { state: CityLoadState; error: string | null; onRetry: () => void }) {
  if (!error || !['offline', 'error', 'sign-in-required'].includes(state)) return null;
  return <View style={styles.errorBox}><Text style={styles.error}>{error}</Text><Pressable onPress={onRetry}><Text style={styles.link}>Retry</Text></Pressable></View>;
}
function More({ state, loaded, total, onPress }: { state: CityLoadState; loaded: number; total: number; onPress: () => void }) {
  return <>
    {state === 'loading-more' && <Message text="Loading more…" loading />}
    {state === 'ready' && loaded < total && <Pressable onPress={onPress} style={styles.more}><Text style={styles.link}>Load more · {loaded} of {total}</Text></Pressable>}
  </>;
}

const styles = StyleSheet.create({
  shell: { flex: 1, backgroundColor: '#fbfcfa', paddingHorizontal: 20, paddingTop: 18 },
  header: { flexDirection: 'row', alignItems: 'center', gap: 10 }, kicker: { color: '#ef704f', fontSize: 9, fontWeight: '800', letterSpacing: 1.5 },
  title: { color: '#153c34', fontSize: 23, fontWeight: '800', marginTop: 3 }, button: { borderRadius: 9, paddingHorizontal: 11, paddingVertical: 8, backgroundColor: '#eef2ee' },
  buttonText: { color: '#31594c', fontSize: 10, fontWeight: '800' }, disabled: { opacity: 0.5 }, rules: { flexDirection: 'row', gap: 8, marginTop: 14 },
  rule: { flex: 1, borderWidth: 1, borderColor: '#dfe7df', borderRadius: 9, padding: 9, alignItems: 'center' }, ruleSelected: { backgroundColor: '#eaf0e7', borderColor: '#31594c' },
  ruleText: { fontSize: 10, color: '#607168' }, ruleTextSelected: { color: '#24463b', fontWeight: '800' }, content: { flex: 1, marginTop: 12 }, label: { color: '#31594c', fontSize: 9, fontWeight: '800', letterSpacing: 1, marginTop: 8, marginBottom: 7 },
  dataset: { borderBottomWidth: 1, borderBottomColor: '#eef1ed', paddingVertical: 11 }, selected: { backgroundColor: '#eef3eb', paddingHorizontal: 8, borderRadius: 8 }, row: { borderBottomWidth: 1, borderBottomColor: '#eef1ed', paddingVertical: 11 },
  rowTitle: { color: '#24463b', fontWeight: '700', fontSize: 12, marginBottom: 3 }, copy: { color: '#7b8981', fontSize: 10, lineHeight: 15 }, search: { backgroundColor: '#f0f3f0', borderRadius: 10, padding: 11, marginVertical: 8 }, filters: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginBottom: 8 },
  filter: { borderRadius: 8, paddingHorizontal: 9, paddingVertical: 6, backgroundColor: '#f0f3f0' }, filterSelected: { backgroundColor: '#31594c' }, filterText: { color: '#607168', fontSize: 9, textTransform: 'capitalize' }, filterTextSelected: { color: '#fff', fontWeight: '700' },
  link: { color: '#31594c', fontWeight: '700', paddingVertical: 8 }, more: { alignItems: 'center', padding: 8 }, message: { alignItems: 'center', padding: 12, gap: 6 }, notice: { color: '#665529', backgroundColor: '#fbf5df', padding: 9, borderRadius: 8, fontSize: 10, marginVertical: 5 },
  errorBox: { backgroundColor: '#fbece9', padding: 10, borderRadius: 8, marginVertical: 8 }, error: { color: '#a33d32', fontSize: 10 }, node: { flexDirection: 'row', alignItems: 'center', gap: 8, borderBottomWidth: 1, borderBottomColor: '#eef1ed', paddingVertical: 8 },
  smallButton: { paddingHorizontal: 8, paddingVertical: 6, backgroundColor: '#eef2ee', borderRadius: 8 }, section: { marginTop: 14, borderTopWidth: 1, borderTopColor: '#e6ebe5' }, contribution: { flexDirection: 'row', alignItems: 'center', gap: 8, borderBottomWidth: 1, borderBottomColor: '#eef1ed', paddingVertical: 8 },
  manualLabel: { color: '#68519a', fontSize: 10, fontWeight: '800', marginTop: 5 }, manualBox: { backgroundColor: '#f2eff8', padding: 10, borderRadius: 9, marginTop: 8 }, manualActions: { flexDirection: 'row', justifyContent: 'flex-end', alignItems: 'center', gap: 16 },
});
