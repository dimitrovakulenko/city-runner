import { createApiRequest } from './client';
import type { AnalyticsBoard, AnalyticsHandoff, AnalyticsHandoffRequest, AnalyticsPreferences, AnalyticsStatus, AnalyticsTemplateRequest } from './generated';

export function createAnalyticsApi(options: Parameters<typeof createApiRequest>[0]) {
  const request = createApiRequest(options);
  return {
    status: (signal?: AbortSignal) => request<AnalyticsStatus>('/api/analytics', { signal }),
    preferences: (body: AnalyticsPreferences, signal?: AbortSignal) => request<void>('/api/analytics/preferences', { method: 'PUT', body: JSON.stringify(body), signal }),
    handoff: (body: AnalyticsHandoffRequest, signal?: AbortSignal) => request<AnalyticsHandoff>('/api/analytics/handoff', { method: 'POST', body: JSON.stringify(body), signal }),
    copy: (body: AnalyticsTemplateRequest, signal?: AbortSignal) => request<AnalyticsBoard>('/api/analytics/boards', { method: 'POST', body: JSON.stringify(body), signal }),
  };
}
