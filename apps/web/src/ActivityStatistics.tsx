import type { ActivitySummary } from '../../mobile/src/api/generated';
import { formatActivityStatistics } from '../../mobile/src/activityStatistics';

export function ActivityStatistics({ activity, compact = false }: { activity: ActivitySummary; compact?: boolean }) {
  const { distance, time, pace } = formatActivityStatistics(activity);
  if (compact) return <span className="activity-statistics-summary" aria-label={`GPS distance: ${distance}. Recorded time: ${time}. Recorded pace: ${pace}.`}>
    {distance === 'Unknown' ? 'Distance unknown' : distance} · {time === 'Unknown' ? 'Time unknown' : time} · {pace === 'Unknown' ? 'Pace unknown' : pace}
  </span>;
  return <section className="activity-statistics" aria-label="Recorded activity statistics">
    <h3>Recorded statistics</h3>
    <dl><div data-stat="distance"><dt>GPS distance</dt><dd>{distance}</dd></div><div data-stat="time"><dt>Recorded time</dt><dd>{time}</dd></div><div data-stat="pace"><dt>Recorded pace</dt><dd>{pace}</dd></div></dl>
    <p className="note">From recorded GPS segments. Time excludes gaps between segments; unmarked stops stay included.</p>
  </section>;
}
