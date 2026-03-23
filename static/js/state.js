// Simple custom state management
let listeners = [];

export const state = {
  users: [],
  stats: { total_users: 0, active_users: 0, total_plays: 0, today_plays: 0 },
  history: [],
  pagination: { page: 1, totalPages: 1, perPage: 20 },
  filters: { userGuid: '', searchTitle: '', startTime: '', endTime: '' },
  traktStatus: { configured: false, connected: false },
  traktDashboard: { summary: {}, failed_queue: [], sync_states: [] },
  isHistoryLoading: false,
  isTraktSubmitting: false,

  subscribe(listener) {
    listeners.push(listener);
    return () => { listeners = listeners.filter(l => l !== listener); };
  },

  notify() {
    listeners.forEach(listener => listener(this));
  },

  // Updates part of the state and notifies components
  update(payload) {
    let changed = false;
    for (const key in payload) {
      if (this[key] !== payload[key]) {
        this[key] = payload[key];
        changed = true;
      }
    }
    if (changed) {
      this.notify();
    }
  }
};
