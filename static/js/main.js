import { state } from './state.js';
import { api } from './api.js';
import { initThemeToggle } from './components/ThemeToggle.js';
import { renderStats } from './components/Stats.js';
import { initFilters } from './components/Filters.js';
import { initHistoryList } from './components/HistoryList.js';
import { initTraktPanel } from './components/TraktPanel.js';

// Init All
document.addEventListener('DOMContentLoaded', async () => {
    // 1. Dark Mode Toggle
    initThemeToggle();

    // 2. Setup rendering logic hooks to our state
    renderStats(document.getElementById('stats-container'));
    
    // 3. Initiate Components
    initFilters('filters-container', async (isClear) => {
        fetchHistoryData();
    });
    
    initHistoryList('history-container', 'pagination-container', () => {
        fetchHistoryData();
    });

    initTraktPanel('trakt-container');
    
    // 4. Load initial global data
    try {
        const [users, stats, tStatus, tDash] = await Promise.all([
            api.getUsers(),
            api.getStats(),
            api.getTraktStatus(),
            api.getTraktDashboard()
        ]);
        
        state.update({
             users: users,
             stats: stats,
             traktStatus: tStatus,
             traktDashboard: tDash
        });
        
    } catch (e) {
        console.error("Initial data load failed: ", e);
    }
    
    // 5. Initial fetch of history
    fetchHistoryData();
});

async function fetchHistoryData() {
    state.update({ isHistoryLoading: true });
    
    try {
        const res = await api.getPlayHistory({
             page: state.pagination.page,
             per_page: state.pagination.perPage,
             user_guid: state.filters.userGuid,
             search_title: state.filters.searchTitle,
             start_time: state.filters.startTime,
             end_time: state.filters.endTime
        });
        
        state.update({
            history: res.data || res.items || [],
            pagination: { ...state.pagination, totalPages: res.pages || 1 },
            isHistoryLoading: false
        });
    } catch (e) {
        console.error("Failed to fetch history:", e);
        state.update({ history: [], isHistoryLoading: false });
    }
}
