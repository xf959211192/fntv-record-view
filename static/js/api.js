export const api = {
  async get(url) {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`GET ${url} failed with status ${response.status}`);
    }
    return response.json();
  },

  async post(url, body) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || data.message || `POST ${url} failed`);
    }
    return data;
  },

  // Users & Stats
  getMeta: () => api.get('/api/meta'),
  getUsers: () => api.get('/api/users'),
  getStats: () => api.get('/api/stats'),
  getPlayHistory: (params) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => { if (v) query.append(k, v); });
    return api.get(`/api/play_history?${query.toString()}`);
  },

  // Trakt Sync Operations
  getTraktStatus: () => api.get('/api/trakt/status'),
  getTraktDashboard: (limit = 20) => api.get(`/api/trakt/dashboard?limit=${limit}`),
  updateTraktSettings: (data) => api.post('/api/trakt/settings', data),
  traktSync: (payload) => api.post('/api/trakt/sync', payload),
  traktDeviceStart: () => api.post('/api/trakt/device/start', {}),
  traktDeviceStatus: (sessionId) => api.get(`/api/trakt/device/status?session_id=${encodeURIComponent(sessionId)}`),
  rematchFailedQueue: (userGuid, itemGuid) => api.post('/api/trakt/failed_queue/rematch', { user_guid: userGuid, item_guid: itemGuid }),
  manualResolveQueue: (payload) => api.post('/api/trakt/failed_queue/manual', payload),
  syncFailedQueueCandidate: (userGuid, itemGuid) => api.post('/api/trakt/failed_queue/sync_candidate', { user_guid: userGuid, item_guid: itemGuid })
};
