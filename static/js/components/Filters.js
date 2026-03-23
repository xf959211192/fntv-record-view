import { state } from '../state.js';
import { api } from '../api.js';
import { debounce } from '../utils.js';

export function initFilters(containerId, onSearch) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const handleSearch = debounce(() => {
    state.pagination.page = 1;
    onSearch();
  }, 400);

  const performImmediateSearch = () => {
    state.pagination.page = 1;
    onSearch();
  };

  const clearFilters = () => {
    document.getElementById('search-title').value = '';
    document.getElementById('start-time').value = '';
    document.getElementById('end-time').value = '';
    document.getElementById('user-select').value = '';
    
    // Update state directly without immediate trigger
    state.filters = { userGuid: '', searchTitle: '', startTime: '', endTime: '' };
    state.pagination.page = 1;
    onSearch(true); // pass clear flag if needed
  };

  // Bind Listeners
  document.getElementById('search-title').addEventListener('input', (e) => {
    state.filters.searchTitle = e.target.value.trim();
    handleSearch();
  });

  const timeInputs = ['start-time', 'end-time'];
  timeInputs.forEach(id => {
    document.getElementById(id).addEventListener('change', (e) => {
      state.filters[id === 'start-time' ? 'startTime' : 'endTime'] = e.target.value;
      performImmediateSearch();
    });
  });

  document.getElementById('user-select').addEventListener('change', (e) => {
    state.filters.userGuid = e.target.value;
    performImmediateSearch();
  });

  document.getElementById('per-page').addEventListener('change', (e) => {
    state.pagination.perPage = parseInt(e.target.value, 10);
    performImmediateSearch();
  });

  document.getElementById('btn-search').addEventListener('click', performImmediateSearch);
  document.getElementById('btn-clear').addEventListener('click', clearFilters);

  const actionRow = container.querySelector('.flex.gap-4.mt-6');
  if (actionRow && !document.getElementById('btn-refresh-db')) {
    const refreshBtn = document.createElement('button');
    refreshBtn.id = 'btn-refresh-db';
    refreshBtn.className = 'btn btn-secondary flex-1';
    refreshBtn.textContent = '手动读取数据库';
    refreshBtn.addEventListener('click', async () => {
      refreshBtn.disabled = true;
      const originalText = refreshBtn.textContent;
      refreshBtn.textContent = '刷新中...';

      try {
        const result = await api.refreshDatabase();
        performImmediateSearch();
        alert(result.message || '数据库副本已刷新');
      } catch (error) {
        alert(`刷新数据库失败: ${error.message}`);
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.textContent = originalText;
      }
    });
    actionRow.appendChild(refreshBtn);
  }

  // Subscribe to populate Users dropdown
  state.subscribe((s) => {
    const userSelect = document.getElementById('user-select');
    if (userSelect && Object.keys(userSelect.options).length <= 1 && s.users.length) {
      const currentVal = userSelect.value;
      userSelect.innerHTML = '<option value="">所有用户</option>';
      s.users.forEach(user => {
        const opt = document.createElement('option');
        opt.value = user.guid;
        opt.textContent = `${user.username}${user.is_admin ? ' (管理员)' : ''}`;
        userSelect.appendChild(opt);
      });
      userSelect.value = currentVal;
    }
  });
}
