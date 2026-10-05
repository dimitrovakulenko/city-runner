import { useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, SafeAreaView, ScrollView, StatusBar, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { Camera, GeoJSONSource, Layer, Map } from '@maplibre/maplibre-react-native';
import type { CameraRef } from '@maplibre/maplibre-react-native';
import type { Feature, MultiLineString } from 'geojson';

type ActivitySummary = { id: string; name: string; date: string; type: string };
type ActivityDetail = ActivitySummary & { tracks: number[][][]; timestamps: (string | null)[][]; bounds: [[number, number], [number, number]] | null };
type DemoState = 'loading' | 'ready' | 'empty' | 'error';

// Mirrors the FastAPI activity list and detail contracts. Large IDs remain strings.
const fixtures: ActivityDetail[] = [
  { id: '1042', name: 'Along the canals', date: '2026-10-04', type: 'running', tracks: [[[3.716, 51.055], [3.718, 51.057], [3.722, 51.056], [3.725, 51.06], [3.729, 51.061]], [[3.729, 51.061], [3.732, 51.063], [3.736, 51.062]]], timestamps: [['2026-10-04T07:12:00Z', '2026-10-04T07:15:00Z', '2026-10-04T07:18:00Z', '2026-10-04T07:21:00Z', '2026-10-04T07:25:00Z'], ['2026-10-04T07:28:00Z', '2026-10-04T07:31:00Z', '2026-10-04T07:34:00Z']], bounds: [[3.716, 51.055], [3.736, 51.063]] },
  { id: '1041', name: 'Sunday city loop', date: '2026-10-02', type: 'running', tracks: [[[3.704, 51.049], [3.709, 51.052], [3.713, 51.05], [3.718, 51.055], [3.723, 51.053]]], timestamps: [['2026-10-02T09:02:00Z', '2026-10-02T09:08:00Z', '2026-10-02T09:14:00Z', '2026-10-02T09:21:00Z', '2026-10-02T09:29:00Z']], bounds: [[3.704, 51.049], [3.723, 51.055]] },
  { id: '-9007199254740993', name: 'Evening ride', date: '2026-09-29', type: 'cycling', tracks: [[[3.692, 51.061], [3.703, 51.065], [3.716, 51.063], [3.729, 51.068]]], timestamps: [['2026-09-29T17:20:00Z', '2026-09-29T17:28:00Z', '2026-09-29T17:36:00Z', '2026-09-29T17:44:00Z']], bounds: [[3.692, 51.061], [3.729, 51.068]] },
  { id: '1039', name: 'Park paths', date: '2026-09-26', type: 'walking', tracks: [[[3.731, 51.048], [3.735, 51.05], [3.739, 51.049], [3.742, 51.052]]], timestamps: [['2026-09-26T14:03:00Z', '2026-09-26T14:09:00Z', '2026-09-26T14:17:00Z', '2026-09-26T14:24:00Z']], bounds: [[3.731, 51.048], [3.742, 51.052]] },
];
const summaries: ActivitySummary[] = fixtures.map(({ id, name, date, type }) => ({ id, name, date, type }));
const demoStyle = 'https://demotiles.maplibre.org/style.json';

export default function App() {
  const { width } = useWindowDimensions();
  const tablet = width >= 850;
  const [state, setState] = useState<DemoState>('loading');
  const [selected, setSelected] = useState<ActivityDetail | null>(null);
  const [zoom, setZoom] = useState(12.5);
  const cameraRef = useRef<CameraRef>(null);

  useEffect(() => {
    const timer = setTimeout(() => setState('ready'), 650);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => setZoom(selected ? 13 : 12.5), [selected?.id]);

  const track = useMemo<Feature<MultiLineString> | null>(() => {
    if (!selected) return null;
    const coordinates = selected.tracks.filter((segment) => segment.length > 1);
    if (!coordinates.length) return null;
    return { type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates } };
  }, [selected]);

  const chooseState = (next: DemoState) => {
    setState(next);
    if (next === 'loading') setTimeout(() => setState('ready'), 650);
  };
  const zoomBy = (step: number) => {
    const next = Math.max(2, Math.min(20, zoom + step));
    setZoom(next);
    cameraRef.current?.zoomTo(next, { duration: 250 });
  };

  return (
    <SafeAreaView style={styles.safe}>
      <StatusBar barStyle="dark-content" />
      <View style={[styles.shell, tablet && styles.tabletShell]}>
        <View style={[styles.panel, tablet && styles.tabletPanel]}>
          <View style={styles.brandRow}>
            <View style={styles.brandMark}><Text style={styles.brandGlyph}>↗</Text></View>
            <View><Text style={styles.brand}>CITY RUNNER</Text><Text style={styles.subBrand}>YOUR OUTDOOR LOG</Text></View>
            <View style={styles.avatar}><Text style={styles.avatarText}>JD</Text></View>
          </View>
          <View style={styles.titleRow}>
            <View style={{ flex: 1 }}><Text style={styles.eyebrow}>{selected ? 'ACTIVITY DETAIL' : 'YOUR HISTORY'}</Text><Text style={styles.heading}>{selected ? selected.name : 'Activities'}</Text></View>
            {selected && <Pressable onPress={() => setSelected(null)} style={styles.backButton}><Text style={styles.backText}>All activities</Text></Pressable>}
          </View>
          {selected ? <Detail activity={selected} onBack={() => setSelected(null)} compact={tablet} /> : <>
            <View style={styles.summaryCard}>
              <View style={styles.summaryIcon}><Text style={styles.summaryGlyph}>⌖</Text></View>
              <View style={{ flex: 1 }}><Text style={styles.summaryLabel}>GENT, BELGIUM</Text><Text style={styles.summaryValue}>A little more explored</Text></View>
              <Text style={styles.summaryArrow}>↗</Text>
            </View>
            <View style={styles.listHeader}><Text style={styles.listTitle}>RECENT ACTIVITIES</Text><Text style={styles.count}>{state === 'ready' ? '04' : '—'}</Text></View>
            <View style={styles.statePicker}>
              {(['ready', 'empty', 'error'] as const).map((item) => <Pressable key={item} onPress={() => chooseState(item)} style={[styles.stateChip, state === item && styles.stateChipActive]}><Text style={[styles.stateChipText, state === item && styles.stateChipTextActive]}>{item === 'ready' ? 'Loaded' : item === 'empty' ? 'Empty' : 'Error'}</Text></Pressable>)}
            </View>
            <ScrollView contentContainerStyle={styles.listContent} showsVerticalScrollIndicator={false}>
              {state === 'loading' && <StateCard title="Loading activities" message="Getting your latest routes ready." loading />}
              {state === 'empty' && <StateCard title="No activities yet" message="Import a run or walk to see your tracks here." action="View map" onPress={() => chooseState('ready')} />}
              {state === 'error' && <StateCard title="Couldn't load activities" message="Check your connection, then try again." action="Try again" onPress={() => chooseState('loading')} />}
              {state === 'ready' && summaries.map((item, index) => <ActivityRow key={item.id} activity={item} index={index} onPress={() => setSelected(fixtures.find((detail) => detail.id === item.id) ?? null)} />)}
            </ScrollView>
          </>}
          <View style={styles.footer}><View style={styles.onlineDot} /><Text style={styles.footerText}>DEMO DATA · LOCAL FIXTURES</Text><Text style={styles.footerMore}>•••</Text></View>
        </View>
        <View style={[styles.mapPanel, tablet && styles.tabletMap]}>
          <Map style={StyleSheet.absoluteFill} mapStyle={demoStyle}>
            <Camera ref={cameraRef} key={selected?.id ?? 'overview'} initialViewState={{ center: selected?.bounds ? [(selected.bounds[0][0] + selected.bounds[1][0]) / 2, (selected.bounds[0][1] + selected.bounds[1][1]) / 2] : [3.72, 51.055], zoom: selected ? 13 : 12.5 }} />
            {track && <GeoJSONSource id="selected-track" data={track}><Layer id="track-casing" type="line" style={{ lineColor: '#ffffff', lineWidth: 8, lineOpacity: 0.9 }} /><Layer id="selected-track-line" type="line" style={{ lineColor: '#f16d4d', lineWidth: 4 }} /></GeoJSONSource>}
          </Map>
          <View style={styles.mapTopPill}><View style={styles.mapStatusDot} /><Text style={styles.mapTopText}>GENT · MAP PREVIEW</Text></View>
          <View style={styles.mapControls}><Pressable accessibilityRole="button" accessibilityLabel="Zoom in" onPress={() => zoomBy(1)} style={styles.mapControl}><Text style={styles.controlText}>＋</Text></Pressable><Pressable accessibilityRole="button" accessibilityLabel="Zoom out" onPress={() => zoomBy(-1)} style={styles.mapControl}><Text style={styles.controlText}>−</Text></Pressable></View>
          {selected && <View style={styles.mapCaption}><View style={styles.captionIcon}><Text style={styles.captionGlyph}>{activityGlyph(selected.type)}</Text></View><View style={{ flex: 1 }}><Text style={styles.captionTitle}>{selected.name}</Text><Text style={styles.captionSub}>{formatDate(selected.date)} · {selected.tracks.reduce((n, segment) => n + segment.length, 0)} GPS points</Text></View><View style={styles.captionDot} />
          </View>}
          <View style={styles.mapAttribution}><Text style={styles.attributionText}>MapLibre · demo style</Text></View>
        </View>
      </View>
    </SafeAreaView>
  );
}

function Detail({ activity, onBack, compact }: { activity: ActivityDetail; onBack: () => void; compact: boolean }) {
  const points = activity.tracks.reduce((count, segment) => count + segment.length, 0);
  const segments = activity.tracks.length;
  return <ScrollView style={{ flex: 1 }} contentContainerStyle={[styles.detailContent, compact && styles.detailTablet]} showsVerticalScrollIndicator={false}>
    <View style={styles.detailHero}><View style={styles.detailIcon}><Text style={styles.detailGlyph}>{activityGlyph(activity.type)}</Text></View><View><Text style={styles.detailDate}>{formatDate(activity.date)}</Text><Text style={styles.detailType}>{activity.type.toUpperCase()}</Text></View></View>
    <View style={styles.metrics}><Metric value={`${points}`} label="GPS POINTS" /><View style={styles.metricDivider} /><Metric value={`${segments}`} label="TRACK PARTS" /><View style={styles.metricDivider} /><Metric value={activity.bounds ? 'GPS' : '—'} label="LOCATION" /></View>
    <View style={styles.detailSection}><Text style={styles.sectionLabel}>TRACK</Text><Text style={styles.detailCopy}>The selected route is highlighted on the map. Track segments are shown separately to preserve GPS gaps.</Text></View>
    <View style={styles.trackInfo}><View style={styles.trackLineSample} /><View style={{ flex: 1 }}><Text style={styles.trackInfoTitle}>Activity track</Text><Text style={styles.trackInfoSub}>{segments} {segments === 1 ? 'segment' : 'segments'} · {points} samples</Text></View><Text style={styles.trackInfoArrow}>↗</Text></View>
    {!compact && <Pressable style={styles.outlineButton} onPress={onBack}><Text style={styles.outlineButtonText}>←  Back to activities</Text></Pressable>}
  </ScrollView>;
}

function ActivityRow({ activity, index, onPress }: { activity: ActivitySummary; index: number; onPress: () => void }) {
  return <Pressable onPress={onPress} style={styles.activityRow}>
    <View style={[styles.rowIcon, index % 2 === 1 && styles.rowIconAlt]}><Text style={styles.rowGlyph}>{activityGlyph(activity.type)}</Text></View>
    <View style={{ flex: 1 }}><Text style={styles.activityName} numberOfLines={1}>{activity.name}</Text><Text style={styles.activityMeta}>{formatDate(activity.date)}  ·  {activity.type}</Text></View>
    <View style={styles.rowArrow}><Text style={styles.rowArrowText}>↗</Text></View>
  </Pressable>;
}

function StateCard({ title, message, loading, action, onPress }: { title: string; message: string; loading?: boolean; action?: string; onPress?: () => void }) {
  return <View style={styles.stateCard}>{loading ? <ActivityIndicator color="#ef704f" /> : <View style={styles.stateIcon}><Text style={styles.stateIconText}>{title.startsWith('No') ? '＋' : '!'}</Text></View>}<Text style={styles.stateTitle}>{title}</Text><Text style={styles.stateMessage}>{message}</Text>{action && <Pressable onPress={onPress} style={styles.retryButton}><Text style={styles.retryText}>{action}  →</Text></Pressable>}</View>;
}

function Metric({ value, label }: { value: string; label: string }) { return <View style={{ flex: 1 }}><Text style={styles.metricValue}>{value}</Text><Text style={styles.metricLabel}>{label}</Text></View>; }
function activityGlyph(type: string) { return type === 'cycling' ? '↗' : type === 'walking' ? '⌁' : '↟'; }
function formatDate(value: string) { const date = new Date(`${value}T12:00:00`); return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }); }

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: '#f4f6f3' }, shell: { flex: 1 }, tabletShell: { flexDirection: 'row', padding: 18, gap: 18 },
  panel: { flex: 1, minHeight: 330, backgroundColor: '#fbfcfa', borderTopLeftRadius: 25, borderTopRightRadius: 25, paddingHorizontal: 22, paddingTop: 14, zIndex: 2 }, tabletPanel: { flex: 0, width: 390, borderRadius: 23, paddingHorizontal: 25, paddingTop: 21, elevation: 3 },
  brandRow: { height: 44, flexDirection: 'row', alignItems: 'center', gap: 10 }, brandMark: { height: 34, width: 34, borderRadius: 11, backgroundColor: '#143e35', alignItems: 'center', justifyContent: 'center' }, brandGlyph: { color: '#d4f064', fontSize: 21, fontWeight: '800' }, brand: { color: '#143e35', fontSize: 12, letterSpacing: 1.5, fontWeight: '900' }, subBrand: { color: '#94a19a', fontSize: 8, letterSpacing: 1.4, marginTop: 2, fontWeight: '700' }, avatar: { marginLeft: 'auto', width: 34, height: 34, borderRadius: 17, backgroundColor: '#e9eee8', alignItems: 'center', justifyContent: 'center' }, avatarText: { color: '#28594b', fontSize: 11, fontWeight: '800' },
  titleRow: { flexDirection: 'row', alignItems: 'center', marginTop: 25, marginBottom: 17 }, eyebrow: { color: '#ef704f', fontSize: 9, letterSpacing: 1.6, fontWeight: '800', marginBottom: 5 }, heading: { color: '#153c34', fontSize: 25, letterSpacing: -0.6, fontWeight: '800' }, backButton: { paddingHorizontal: 12, paddingVertical: 9, borderRadius: 12, backgroundColor: '#eef2ee' }, backText: { color: '#31594c', fontWeight: '700', fontSize: 11 },
  summaryCard: { backgroundColor: '#eaf0e7', borderRadius: 16, minHeight: 72, flexDirection: 'row', alignItems: 'center', paddingHorizontal: 13, gap: 11, marginBottom: 24 }, summaryIcon: { height: 40, width: 40, borderRadius: 13, backgroundColor: '#dce8d9', alignItems: 'center', justifyContent: 'center' }, summaryGlyph: { color: '#31715c', fontSize: 23, fontWeight: '700' }, summaryLabel: { color: '#7a9083', fontSize: 8, fontWeight: '800', letterSpacing: 1.2, marginBottom: 4 }, summaryValue: { color: '#264f43', fontSize: 12, fontWeight: '700' }, summaryArrow: { color: '#55806b', fontSize: 19, marginRight: 5 },
  listHeader: { flexDirection: 'row', alignItems: 'center', marginBottom: 9 }, listTitle: { color: '#8b9991', fontSize: 9, fontWeight: '800', letterSpacing: 1.3 }, count: { marginLeft: 'auto', color: '#a2ada5', fontSize: 10, fontWeight: '700' }, statePicker: { flexDirection: 'row', alignSelf: 'flex-start', gap: 5, padding: 3, backgroundColor: '#f0f3f0', borderRadius: 10, marginBottom: 9 }, stateChip: { paddingVertical: 6, paddingHorizontal: 10, borderRadius: 7 }, stateChipActive: { backgroundColor: '#fff', shadowColor: '#243c31', shadowOpacity: 0.07, shadowRadius: 4, elevation: 1 }, stateChipText: { color: '#8a9790', fontSize: 10, fontWeight: '700' }, stateChipTextActive: { color: '#294d40' }, listContent: { paddingBottom: 8 },
  activityRow: { minHeight: 68, flexDirection: 'row', alignItems: 'center', gap: 11, borderBottomWidth: 1, borderBottomColor: '#eef1ed', paddingVertical: 9, paddingHorizontal: 2 }, activityRowActive: { backgroundColor: '#f4f7f3', borderRadius: 13, borderBottomColor: 'transparent', paddingHorizontal: 8 }, rowIcon: { width: 41, height: 41, borderRadius: 13, backgroundColor: '#fce9e2', alignItems: 'center', justifyContent: 'center' }, rowIconAlt: { backgroundColor: '#e8f0e8' }, rowGlyph: { color: '#e87857', fontSize: 19, fontWeight: '700' }, activityName: { color: '#24463b', fontWeight: '700', fontSize: 13, marginBottom: 5 }, activityMeta: { color: '#8d9b92', fontSize: 10, textTransform: 'capitalize' }, rowArrow: { height: 29, width: 29, borderRadius: 10, backgroundColor: '#f0f3ef', alignItems: 'center', justifyContent: 'center' }, rowArrowActive: { backgroundColor: '#173e35' }, rowArrowText: { color: '#8c9a91', fontSize: 15 }, rowArrowTextActive: { color: '#d7ef71' },
  footer: { height: 39, flexDirection: 'row', alignItems: 'center', gap: 7, borderTopWidth: 1, borderTopColor: '#eef1ed' }, onlineDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: '#9fb873' }, footerText: { color: '#9ba79f', fontSize: 8, fontWeight: '800', letterSpacing: 1.1 }, footerMore: { marginLeft: 'auto', color: '#9ba79f', letterSpacing: 2, fontWeight: '800' },
  mapPanel: { flex: 1, minHeight: 270, backgroundColor: '#dce5db', overflow: 'hidden' }, tabletMap: { borderRadius: 23 }, mapTopPill: { position: 'absolute', top: 16, left: 16, backgroundColor: '#fbfcfa', paddingHorizontal: 12, paddingVertical: 9, borderRadius: 12, flexDirection: 'row', gap: 7, alignItems: 'center', shadowColor: '#1e392f', shadowOpacity: 0.12, shadowRadius: 10, elevation: 2 }, mapStatusDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: '#ef704f' }, mapTopText: { color: '#31594c', fontSize: 9, fontWeight: '800', letterSpacing: 1 }, mapControls: { position: 'absolute', right: 15, top: 16, borderRadius: 12, overflow: 'hidden', backgroundColor: '#fbfcfa', elevation: 2 }, mapControl: { height: 38, width: 38, alignItems: 'center', justifyContent: 'center', borderBottomWidth: 1, borderBottomColor: '#edf0eb' }, controlText: { color: '#31594c', fontSize: 19, fontWeight: '600' }, mapCaption: { position: 'absolute', bottom: 28, left: 16, right: 16, minHeight: 67, borderRadius: 16, backgroundColor: '#fbfcfa', flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, gap: 10, elevation: 3, shadowColor: '#18372d', shadowOpacity: 0.14, shadowRadius: 12 }, captionIcon: { width: 38, height: 38, borderRadius: 12, backgroundColor: '#fce9e2', alignItems: 'center', justifyContent: 'center' }, captionGlyph: { color: '#e87857', fontSize: 18, fontWeight: '700' }, captionTitle: { color: '#25483d', fontSize: 12, fontWeight: '800' }, captionSub: { color: '#8d9b92', fontSize: 10, marginTop: 4 }, captionDot: { width: 9, height: 9, borderRadius: 5, backgroundColor: '#ef704f' }, mapAttribution: { position: 'absolute', bottom: 6, right: 8, paddingHorizontal: 6, paddingVertical: 3, borderRadius: 5, backgroundColor: '#fbfcfacc' }, attributionText: { color: '#607168', fontSize: 8 },
  detailContent: { paddingBottom: 12 }, detailTablet: { paddingBottom: 20 }, detailHero: { flexDirection: 'row', alignItems: 'center', gap: 12, marginBottom: 19 }, detailIcon: { height: 52, width: 52, borderRadius: 17, backgroundColor: '#fce9e2', alignItems: 'center', justifyContent: 'center' }, detailGlyph: { color: '#e87857', fontSize: 24, fontWeight: '700' }, detailDate: { color: '#31594c', fontWeight: '800', fontSize: 14 }, detailType: { color: '#99a49c', fontSize: 9, letterSpacing: 1.1, fontWeight: '800', marginTop: 5 }, metrics: { flexDirection: 'row', backgroundColor: '#f1f4f0', borderRadius: 14, padding: 14, alignItems: 'center', marginBottom: 24 }, metricValue: { color: '#25483d', fontSize: 18, fontWeight: '800', marginBottom: 4 }, metricLabel: { color: '#96a39a', fontSize: 8, fontWeight: '800', letterSpacing: 0.8 }, metricDivider: { width: 1, height: 28, backgroundColor: '#dfe6de', marginHorizontal: 11 }, detailSection: { marginBottom: 18 }, sectionLabel: { color: '#8b9991', fontSize: 9, letterSpacing: 1.3, fontWeight: '800', marginBottom: 8 }, detailCopy: { color: '#728178', fontSize: 12, lineHeight: 19 }, trackInfo: { borderWidth: 1, borderColor: '#e9eee8', minHeight: 62, borderRadius: 13, flexDirection: 'row', alignItems: 'center', padding: 12, gap: 11 }, trackLineSample: { width: 28, height: 4, backgroundColor: '#ef704f', borderRadius: 2, transform: [{ rotate: '-22deg' }] }, trackInfoTitle: { color: '#34594b', fontSize: 11, fontWeight: '800' }, trackInfoSub: { color: '#96a39a', fontSize: 10, marginTop: 4 }, trackInfoArrow: { color: '#95a39a', fontSize: 17 }, outlineButton: { marginTop: 17, borderWidth: 1, borderColor: '#dfe7df', borderRadius: 12, paddingVertical: 12, alignItems: 'center' }, outlineButtonText: { color: '#456757', fontWeight: '700', fontSize: 11 },
  stateCard: { alignItems: 'center', paddingHorizontal: 16, paddingVertical: 28 }, stateIcon: { width: 38, height: 38, borderRadius: 13, backgroundColor: '#fce9e2', alignItems: 'center', justifyContent: 'center', marginBottom: 10 }, stateIconText: { color: '#df7758', fontSize: 19, fontWeight: '700' }, stateTitle: { color: '#315447', fontSize: 13, fontWeight: '800', marginTop: 12, marginBottom: 6 }, stateMessage: { color: '#8b9991', fontSize: 11, textAlign: 'center', lineHeight: 17, maxWidth: 230 }, retryButton: { paddingHorizontal: 13, paddingVertical: 9, borderRadius: 10, backgroundColor: '#eaf0e7', marginTop: 13 }, retryText: { color: '#31594c', fontSize: 10, fontWeight: '800' },
});
