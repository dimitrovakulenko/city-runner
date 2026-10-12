import type { ActivityDetail, ActivitySummary } from './api/generated';

export interface FormattedActivityStatistics {
  distance: string;
  time: string;
  pace: string;
}

type ActivityStatistics = Pick<ActivitySummary, 'distance_m' | 'duration_s' | 'pace_s_per_km'> |
  Pick<ActivityDetail, 'distance_m' | 'duration_s' | 'pace_s_per_km'>;

export function formatActivityStatistics(activity: ActivityStatistics): FormattedActivityStatistics {
  return {
    distance: formatDistance(activity.distance_m),
    time: formatTime(activity.duration_s),
    pace: formatPace(activity.pace_s_per_km),
  };
}

function validNonnegative(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

function formatDistance(meters: number | null | undefined): string {
  if (!validNonnegative(meters)) return 'Unknown';
  if (meters === 0) return '0.00 km';
  const kilometers = meters / 1_000;
  return kilometers < 0.005 ? '<0.01 km' : `${kilometers.toFixed(2)} km`;
}

function formatTime(seconds: number | null | undefined): string {
  if (!validNonnegative(seconds)) return 'Unknown';
  if (seconds === 0) return '0:00';
  const rounded = Math.round(seconds);
  if (rounded === 0) return '<0:01';
  const hours = Math.floor(rounded / 3_600);
  const minutes = Math.floor((rounded % 3_600) / 60);
  const remainder = rounded % 60;
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(remainder)}` : `${minutes}:${pad(remainder)}`;
}

function formatPace(secondsPerKilometer: number | null | undefined): string {
  if (!validNonnegative(secondsPerKilometer) || secondsPerKilometer === 0) return 'Unknown';
  const rounded = Math.round(secondsPerKilometer);
  if (rounded === 0) return '<0:01/km';
  const minutes = Math.floor(rounded / 60);
  const seconds = rounded % 60;
  return `${minutes}:${pad(seconds)}/km`;
}

function pad(value: number): string { return String(value).padStart(2, '0'); }
