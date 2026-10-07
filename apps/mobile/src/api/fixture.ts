import type { ActivityApi } from './client';
import type { ActivityDetail } from './generated';

const activities: ActivityDetail[] = [
  { id: '1042', name: 'Along the canals', date: '2026-10-04', type: 'running', processed: true, unmapped_points: 0, tracks: [[[3.716, 51.055], [3.718, 51.057], [3.722, 51.056], [3.725, 51.06]], [[3.729, 51.061], [3.732, 51.063]]], timestamps: [['2026-10-04T07:12:00Z', '2026-10-04T07:15:00Z', '2026-10-04T07:18:00Z', '2026-10-04T07:21:00Z'], ['2026-10-04T07:28:00Z', '2026-10-04T07:31:00Z']], bounds: [[3.716, 51.055], [3.732, 51.063]] },
  { id: '1041', name: 'Sunday city loop', date: '2026-10-02', type: 'running', processed: true, unmapped_points: 0, tracks: [[[3.704, 51.049], [3.709, 51.052], [3.713, 51.05]]], timestamps: [['2026-10-02T09:02:00Z', '2026-10-02T09:08:00Z', '2026-10-02T09:14:00Z']], bounds: [[3.704, 51.049], [3.713, 51.052]] },
  { id: '-9007199254740993', name: 'Evening ride', date: '2026-09-29', type: 'cycling', processed: true, unmapped_points: 0, tracks: [[[3.692, 51.061], [3.703, 51.065], [3.716, 51.063]]], timestamps: [['2026-09-29T17:20:00Z', '2026-09-29T17:28:00Z', '2026-09-29T17:36:00Z']], bounds: [[3.692, 51.061], [3.716, 51.065]] },
];

export const fixtureApi: ActivityApi = {
  async listActivities({ page = 1, pageSize = 20, query = '' } = {}) {
    const matching = activities.filter((item) => item.name.toLowerCase().includes(query.toLowerCase()));
    const start = (page - 1) * pageSize;
    return {
      items: matching.slice(start, start + pageSize).map(({ id, name, date, type, processed, unmapped_points }) => ({ id, name, date, type, processed, unmapped_points })),
      page, page_size: pageSize, total: matching.length,
    };
  },
  async getActivity(id) {
    const result = activities.find((item) => item.id === id);
    if (!result) throw new Error('Activity not found.');
    return result;
  },
  async getActivityImpact() { throw new Error('Fixture mode does not provide activity impact.'); },
  async getMe() { throw new Error('Fixture mode does not provide sign-in.'); },
  async createChallenge() { throw new Error('Fixture mode does not provide sign-in.'); },
  async exchange() { throw new Error('Fixture mode does not provide sign-in.'); },
  async logout() { throw new Error('Fixture mode does not provide sign-in.'); },
};
