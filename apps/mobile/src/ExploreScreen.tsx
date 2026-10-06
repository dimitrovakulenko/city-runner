import { useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, AppState, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { Camera, GeoJSONSource, Layer, Map } from '@maplibre/maplibre-react-native';
import type { MapRef, ViewStateChangeEvent } from '@maplibre/maplibre-react-native';
import type { Feature, FeatureCollection, Geometry } from 'geojson';
import type { ExploreStore, ExploreState, Viewport } from './exploreStore';
import type { MapResponse } from './api/generated';
import { ImportPanel } from './ImportPanel';
import type { ImportStore } from './importStore';

const INITIAL_VIEW: Viewport = { bbox: [3.6, 51, 3.85, 51.1], zoom: 12 };
const FIXTURE_STYLE = 'https://demotiles.maplibre.org/style.json';
const DEFAULT_STYLE = 'https://tiles.openfreemap.org/styles/liberty';

export function ExploreScreen({ store, importStore, fixtureMode, enabled, accountGeneration, tablet }: { store: ExploreStore; importStore: ImportStore; fixtureMode: boolean; enabled: boolean; accountGeneration: number; tablet: boolean }) {
  const [state, setState] = useState<ExploreState>(store.getState());
  const [styleError, setStyleError] = useState(false);
  const [styleRevision, setStyleRevision] = useState(0);
  const mapRef = useRef<MapRef>(null);
  const changeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const styleUrl = fixtureMode ? FIXTURE_STYLE : process.env.EXPO_PUBLIC_MAP_STYLE_URL?.trim() || DEFAULT_STYLE;

  useEffect(() => {
    const unsubscribe = store.subscribe(setState);
    if (enabled && !fixtureMode) void store.refreshProgress();
    return () => { unsubscribe(); if (changeTimer.current) clearTimeout(changeTimer.current); };
  }, [accountGeneration, enabled, fixtureMode, store]);

  useEffect(() => {
    const subscription = AppState.addEventListener('change', (status) => {
      if (status !== 'active' || fixtureMode || !enabled) return;
      const viewport = store.getState().viewport;
      if (viewport) void store.refreshViewport(viewport);
      else void store.refreshProgress();
    });
    const timer = setInterval(() => {
      if (!fixtureMode && enabled && AppState.currentState === 'active') void store.refreshForeground();
    }, 2500);
    return () => { subscription.remove(); clearInterval(timer); };
  }, [enabled, fixtureMode, store]);

  const collections = useMemo(() => toCollections(state.map), [state.map]);
  const changeRegion = (event: { nativeEvent: ViewStateChangeEvent }) => {
    if (fixtureMode || !enabledRef.current) return;
    const { bounds, zoom } = event.nativeEvent;
    const generation = store.getAccountGeneration();
    store.invalidateViewport();
    if (changeTimer.current) clearTimeout(changeTimer.current);
    changeTimer.current = setTimeout(() => {
      if (enabledRef.current && generation === store.getAccountGeneration()) void store.refreshViewport({ bbox: bounds, zoom });
    }, 350);
  };
  const readInitialViewport = async () => {
    if (fixtureMode || !enabledRef.current) return;
    const generation = store.getAccountGeneration();
    if (!mapRef.current) return;
    try {
      const [bbox, zoom] = await Promise.all([mapRef.current.getBounds(), mapRef.current.getZoom()]);
      if (!enabledRef.current || generation !== store.getAccountGeneration()) return;
      void store.refreshViewport({ bbox, zoom });
    } catch {
      if (enabledRef.current && generation === store.getAccountGeneration()) void store.refreshViewport(INITIAL_VIEW);
    }
  };
  const retry = () => { if (fixtureMode || enabledRef.current) void store.retry(); };
  const mapFailure = state.mapStatus === 'offline' || state.mapStatus === 'error';
  const signedOut = state.mapStatus === 'sign-in-required' || state.progressStatus === 'sign-in-required';

  return <View style={[styles.shell, tablet && styles.tablet]}>
    <View style={styles.panel}>
      <View style={styles.headingRow}><View style={{ flex: 1 }}><Text style={styles.kicker}>EXPLORE</Text><Text style={styles.title}>Your coverage</Text></View>
        <Pressable accessibilityRole="button" onPress={retry} disabled={!fixtureMode && !enabled} style={styles.refresh}><Text style={styles.refreshText}>↻ Refresh</Text></Pressable>
      </View>
      <View style={styles.ruleRow}>
        {(['normal', 'strict'] as const).map((rule) => <Pressable key={rule} disabled={!fixtureMode && !enabled} onPress={() => { if (fixtureMode || enabledRef.current) store.setRule(rule); }} style={[styles.rule, state.rule === rule && styles.ruleSelected]}><Text style={[styles.ruleText, state.rule === rule && styles.ruleTextSelected]}>{rule === 'normal' ? 'Normal · 90%' : 'Strict · all nodes'}</Text></Pressable>)}
      </View>
      <ScrollView style={styles.info} contentContainerStyle={styles.infoContent}>
        <ProgressSummary state={state} fixtureMode={fixtureMode} />
        <ImportPanel store={importStore} enabled={enabled} fixtureMode={fixtureMode} accountGeneration={accountGeneration} />
        {state.foregroundPollingStopped && <Text style={styles.copy}>Automatic checks paused. Use Refresh to check again.</Text>}
        {state.progress?.pending_imports ? <Text style={styles.notice}>{state.progress.pending_imports} import(s) still processing.</Text> : null}
        {state.map?.geography_state === 'geography_pending' && <Text style={styles.notice}>Coverage is not available for this map area yet.</Text>}
        {state.map?.node_state === 'pending' && <Text style={styles.notice}>Matching is still processing. Missing nodes will appear when ready.</Text>}
        {state.map?.limits.streets.truncated && <Text style={styles.notice}>Showing a limited street sample for this viewport.</Text>}
        {state.map?.limits.missing_nodes.truncated && <Text style={styles.notice}>Showing a limited sample of missing nodes.</Text>}
        {state.map?.limits.tracks.truncated && <Text style={styles.notice}>Showing a limited activity track sample.</Text>}
        {state.map?.limits.cities.truncated && <Text style={styles.notice}>Showing a limited city sample.</Text>}
        {state.map?.limits.points.truncated && <Text style={styles.notice}>Some map geometry was omitted to keep the response bounded.</Text>}
        {state.map?.limits.geometry_bytes.truncated && <Text style={styles.notice}>Some map geometry was omitted to keep the response bounded.</Text>}
        {state.map?.dataset_truncated && <Text style={styles.notice}>Showing a limited dataset sample.</Text>}
        {state.map?.node_state === 'not-requested' && <Text style={styles.copy}>Zoom in to discover missing nodes.</Text>}
        {state.mapStatus === 'empty' && state.map?.geography_state === 'supported' && <Text style={styles.copy}>No routes or eligible streets in this viewport.</Text>}
        {mapFailure && <StateMessage message={state.mapError ?? 'Could not load this map area.'} action="Retry" onPress={retry} />}
        {!fixtureMode && !enabled && <StateMessage message="Sign in to view private activity and coverage data." />}
        {signedOut && enabled && <StateMessage message="Your session expired. Sign in again to view private activity and coverage data." />}
      </ScrollView>
    </View>
    <View style={styles.mapPanel}>
      <Map key={styleRevision} ref={mapRef} style={StyleSheet.absoluteFill} mapStyle={styleUrl} onDidFinishLoadingMap={() => { setStyleError(false); void readInitialViewport(); }} onDidFailLoadingMap={() => setStyleError(true)} onRegionIsChanging={() => { if (!fixtureMode && enabledRef.current) store.invalidateViewport(); }} onRegionDidChange={changeRegion}>
        <Camera key="explore-camera" initialViewState={{ center: state.viewport ? [(state.viewport.bbox[0] + state.viewport.bbox[2]) / 2, (state.viewport.bbox[1] + state.viewport.bbox[3]) / 2] : [3.72, 51.055], zoom: state.viewport?.zoom ?? 12 }} />
        {!!collections.tracks.features.length && <GeoJSONSource id="activity-tracks" data={collections.tracks}><Layer id="activity-tracks-line" type="line" style={{ lineColor: '#374f67', lineWidth: 3 }} /><Layer id="activity-track-points" type="circle" style={{ circleColor: '#374f67', circleRadius: 3 }} /></GeoJSONSource>}
        {!!collections.streets.features.length && <GeoJSONSource id="coverage-streets" data={collections.streets}><Layer id="coverage-streets-line" type="line" style={{ lineColor: ['case', ['==', ['get', 'manual_completed'], true], '#7956a8', ['==', ['get', 'completed'], true], '#4b9567', ['==', ['get', 'completed'], false], '#e8874c', '#88948e'], lineWidth: 3 }} /></GeoJSONSource>}
        {!!collections.nodes.features.length && <GeoJSONSource id="missing-nodes" data={collections.nodes}><Layer id="missing-nodes-points" type="circle" style={{ circleColor: '#d8584a', circleRadius: 4, circleStrokeColor: '#ffffff', circleStrokeWidth: 1 }} /></GeoJSONSource>}
      </Map>
      {styleError && <View style={styles.mapUnavailable}><Text style={styles.sectionTitle}>Map is unavailable</Text><Text style={styles.copy}>The street map could not be loaded.</Text><Pressable onPress={() => setStyleRevision((value) => value + 1)}><Text style={styles.link}>Retry map</Text></Pressable></View>}
      {!fixtureMode && enabled && <View style={styles.mapLegend}><Text style={styles.legendText}>GPS complete · green</Text><Text style={styles.manualLegend}>Manual · purple</Text><Text style={styles.incompleteLegend}>Incomplete · orange</Text></View>}
      {styleUrl && state.mapStatus === 'loading' && <View style={styles.mapLoading}><ActivityIndicator color="#ef704f" /></View>}
    </View>
  </View>;
}

function ProgressSummary({ state, fixtureMode }: { state: ExploreState; fixtureMode: boolean }) {
  if (fixtureMode) return <Text style={styles.copy}>Coverage data is unavailable in fixture mode.</Text>;
  const progress = state.progress;
  if (state.progressStatus === 'loading' && !progress) return <StateMessage message="Loading lifetime progress…" loading />;
  if (state.progressStatus === 'offline' || state.progressStatus === 'error') return <StateMessage message={state.progressError ?? 'Could not load lifetime progress.'} />;
  if (!progress) return <Text style={styles.copy}>Lifetime progress is not available yet.</Text>;
  if (progress.state === 'unsupported-geography') return <Text style={styles.notice}>No supported city dataset is active yet.</Text>;
  return <View style={styles.progressCard}>
    <Text style={styles.sectionTitle}>LIFETIME PROGRESS · {progress.rule.toUpperCase()}</Text>
    <Text style={styles.copy}>Normal: 90%, or every node on streets with fewer than 10 eligible nodes. Strict: every node.</Text>
    {progress.datasets.every((dataset) => ['ready', 'not-matched'].includes(dataset.state) && dataset.visited_node_count !== null)
      ? <><Text style={styles.bigNumber}>{progress.datasets.reduce((total, dataset) => total + (dataset.visited_node_count ?? 0), 0).toLocaleString()}</Text><Text style={styles.copy}>visited nodes across active datasets</Text></>
      : <Text style={styles.copy}>Visited-node total is still being calculated.</Text>}
    {progress.datasets.map((dataset) => <View key={dataset.dataset_id} style={styles.dataset}>
      <Text style={styles.datasetName}>{dataset.region} · {dataset.state}</Text>
      {dataset.eligible_streets === null || dataset.completed_streets === null
        ? <Text style={styles.copy}>GPS street progress is still being calculated.</Text>
        : <Text style={styles.copy}>GPS complete: {dataset.completed_streets} / {dataset.eligible_streets}</Text>}
      <Text style={styles.copy}>Effective complete: {dataset.effective_completed_streets ?? 'pending'} / {dataset.eligible_streets ?? 'pending'}</Text>
      <Text style={styles.copy}>Manual labels: {dataset.manual_completed_streets}</Text>
      {dataset.visited_node_count === null && <Text style={styles.copy}>Visited-node count pending.</Text>}
      {dataset.failed_sources ? <Text style={styles.error}>{dataset.failed_sources} source job(s) failed.</Text> : null}
    </View>)}
  </View>;
}

function StateMessage({ message, action, onPress, loading }: { message: string; action?: string; onPress?: () => void; loading?: boolean }) {
  return <View style={styles.stateMessage}>{loading && <ActivityIndicator color="#ef704f" />}<Text style={styles.copy}>{message}</Text>{action && <Pressable onPress={onPress}><Text style={styles.link}>{action}</Text></Pressable>}</View>;
}

function toCollections(map: MapResponse | null): { tracks: FeatureCollection; streets: FeatureCollection; nodes: FeatureCollection } {
  const collection = (features: Feature[]) => ({ type: 'FeatureCollection', features }) as FeatureCollection;
  if (!map) return { tracks: collection([]), streets: collection([]), nodes: collection([]) };
  return {
    tracks: collection(map.tracks.map((track) => ({ type: 'Feature', properties: { id: track.activity_id }, geometry: track.geometry as unknown as Geometry }))),
    streets: collection(map.streets.map((street) => ({ type: 'Feature', properties: { completed: street.completed, manual_completed: street.manual_completed,
      effective_completed: street.effective_completed, manual_reason: street.manual_reason, visited: street.visited_nodes, eligible: street.eligible_nodes, id: street.street_id }, geometry: street.geometry as unknown as Geometry }))),
    nodes: collection(map.missing_nodes.map((node) => ({ type: 'Feature', properties: { id: node.node_id }, geometry: { type: 'Point', coordinates: [node.longitude, node.latitude] } }))),
  };
}

const styles = StyleSheet.create({
  shell: { flex: 1 }, tablet: { flexDirection: 'row' },
  panel: { flex: 1, minHeight: 240, paddingHorizontal: 20, paddingTop: 20, backgroundColor: '#fbfcfa' },
  headingRow: { flexDirection: 'row', alignItems: 'center' }, kicker: { color: '#ef704f', fontSize: 9, fontWeight: '800', letterSpacing: 1.5 },
  title: { color: '#153c34', fontSize: 23, fontWeight: '800', marginTop: 3 },
  refresh: { borderRadius: 9, paddingHorizontal: 10, paddingVertical: 8, backgroundColor: '#eef2ee' }, refreshText: { color: '#31594c', fontWeight: '700', fontSize: 11 },
  ruleRow: { flexDirection: 'row', gap: 8, marginTop: 15 }, rule: { flex: 1, borderWidth: 1, borderColor: '#dfe7df', borderRadius: 9, padding: 9, alignItems: 'center' }, ruleSelected: { backgroundColor: '#eaf0e7', borderColor: '#31594c' }, ruleText: { fontSize: 10, color: '#607168' }, ruleTextSelected: { color: '#24463b', fontWeight: '800' },
  info: { flex: 1, marginTop: 10 }, infoContent: { paddingBottom: 10 }, progressCard: { backgroundColor: '#eef3eb', padding: 14, borderRadius: 14, marginBottom: 12 },
  sectionTitle: { color: '#31594c', fontSize: 10, letterSpacing: 1, fontWeight: '800' }, bigNumber: { color: '#153c34', fontSize: 27, fontWeight: '900', marginTop: 4 }, copy: { color: '#7b8981', fontSize: 11, lineHeight: 17 },
  dataset: { borderTopWidth: 1, borderTopColor: '#dce5db', marginTop: 10, paddingTop: 8, gap: 3 }, datasetName: { color: '#31594c', fontSize: 11, fontWeight: '700', textTransform: 'capitalize' },
  uploadCard: { borderWidth: 1, borderColor: '#e6ebe5', borderRadius: 13, padding: 13, marginBottom: 10, gap: 7 }, action: { backgroundColor: '#173e35', padding: 11, borderRadius: 9, alignItems: 'center' }, actionDisabled: { opacity: 0.65 }, actionText: { color: '#fff', fontSize: 11, fontWeight: '800' },
  uploadItem: { backgroundColor: '#f4f6f3', borderRadius: 10, padding: 10, marginBottom: 8, gap: 3 }, uploadName: { color: '#31594c', fontSize: 11, fontWeight: '700' },
  error: { color: '#a33d32', fontSize: 10, marginTop: 4 }, notice: { color: '#665529', backgroundColor: '#fbf5df', padding: 9, borderRadius: 8, fontSize: 10, marginTop: 7 },
  stateMessage: { paddingVertical: 14, gap: 7, alignItems: 'center' }, link: { color: '#31594c', padding: 7, fontWeight: '700' },
  mapPanel: { flex: 1, minHeight: 230, backgroundColor: '#dce5db', overflow: 'hidden' }, mapUnavailable: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 22, gap: 8 },
  mapLoading: { position: 'absolute', top: 12, right: 12, backgroundColor: '#fff', padding: 8, borderRadius: 10 },
  mapLegend: { position: 'absolute', bottom: 12, left: 12, backgroundColor: '#fbfcfa', borderRadius: 8, padding: 8, gap: 3 },
  legendText: { color: '#4b9567', fontSize: 9, fontWeight: '700' }, manualLegend: { color: '#7956a8', fontSize: 9, fontWeight: '700' }, incompleteLegend: { color: '#c7763e', fontSize: 9, fontWeight: '700' },
});
