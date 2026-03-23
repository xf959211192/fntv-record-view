import { state } from '../state.js';
import { escapeHtml } from '../utils.js';

export function initHistoryList(containerId, paginationId, fetchPage) {
  const container = document.getElementById(containerId);
  const paginationElem = document.getElementById(paginationId);

  if (!container || !paginationElem) return;

  state.subscribe((s) => {
    if (s.isHistoryLoading) {
      container.innerHTML = `
        <div class="p-8 text-center text-muted w-full">
          <div class="spinner"></div>
          <p class="mt-4">正在加载数据...</p>
        </div>
      `;
      return;
    }

    if (!s.history || s.history.length === 0) {
      container.innerHTML = `
        <div class="p-8 text-center text-muted">
          <div class="text-4xl opacity-50 mb-4">📜</div>
          <p>没有找到观看记录</p>
        </div>
      `;
      paginationElem.innerHTML = '';
      return;
    }

    const html = s.history.map((item) => {
      const itemType = item.item_type || item.type || '';
      const isMovie = itemType === 'movie';
      const playProgress = Number(item.play_progress ?? item.progress ?? 0);
      const hasPosition = Number(item.position || 0) > 0;
      const watchState = item.watch_state || (
        playProgress >= 100 ? 'watched' : (
          playProgress > 0 ? 'in_progress' : (
            hasPosition ? 'played' : 'unwatched'
          )
        )
      );

      let badgeHtml = '';
      if (watchState === 'watched') {
        badgeHtml = '<span class="badge badge-success">已观看</span>';
      } else if (watchState === 'in_progress') {
        badgeHtml = `<span class="badge badge-primary">进度 ${playProgress.toFixed(1)}%</span>`;
      } else if (watchState === 'played') {
        badgeHtml = '<span class="badge badge-warning">已播放</span>';
      } else {
        badgeHtml = '<span class="badge badge-info">未观看</span>';
      }

      return `
        <div class="history-item surface flex justify-between items-center animate-fade">
          <div class="item-info flex-1">
            <h4 class="item-title">${escapeHtml(item.title || item.item_title || '')}</h4>
            <div class="flex gap-2 items-center mt-2 flex-wrap">
              <span class="badge-outline">${isMovie ? '电影' : '剧集'}</span>
              ${badgeHtml}
              ${item.app_version ? `<span class="badge-outline">v${escapeHtml(item.app_version)}</span>` : ''}
            </div>

            <div class="progress-bar mt-3">
              <div class="progress-fill" style="width: ${Math.min(playProgress || 0, 100)}%"></div>
            </div>
          </div>
          <div class="item-user text-right shrink-0">
            <div class="user-name">${escapeHtml(item.username || '')}</div>
            <div class="play-time text-sm text-muted">${escapeHtml(item.update_time_display || item.update_time || '')}</div>
          </div>
        </div>
      `;
    }).join('');

    container.innerHTML = `<div class="flex flex-col gap-3">${html}</div>`;
    renderPagination(paginationElem, s.pagination, fetchPage);
  });
}

function renderPagination(elem, pagination, fetchPage) {
  const { page, totalPages } = pagination;
  if (totalPages <= 1) {
    elem.innerHTML = '';
    return;
  }

  const createBtn = (p, text, disabled, active) => {
    return `<button class="btn btn-secondary ${active ? 'btn-primary' : ''}" ${disabled ? 'disabled' : ''} data-page="${p}">${text}</button>`;
  };

  const html = [];
  html.push(createBtn(page - 1, '上一页', page <= 1, false));

  const maxVisible = 5;
  let start = Math.max(1, page - Math.floor(maxVisible / 2));
  let end = Math.min(totalPages, start + maxVisible - 1);
  if (end - start + 1 < maxVisible) {
    start = Math.max(1, end - maxVisible + 1);
  }

  for (let i = start; i <= end; i += 1) {
    html.push(createBtn(i, i, false, i === page));
  }

  html.push(createBtn(page + 1, '下一页', page >= totalPages, false));
  elem.innerHTML = `<div class="flex gap-2 justify-center">${html.join('')}</div>`;

  const buttons = elem.querySelectorAll('button');
  buttons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const newPage = parseInt(btn.getAttribute('data-page') || '0', 10);
      if (newPage !== page && newPage >= 1 && newPage <= totalPages) {
        state.pagination.page = newPage;
        fetchPage();
      }
    });
  });
}
