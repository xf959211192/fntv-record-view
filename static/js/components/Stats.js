import { state } from '../state.js';

export function renderStats(el) {
  state.subscribe((s) => {
    el.innerHTML = `
      <div class="stat-card">
        <div class="number text-primary">${s.stats.total_users || '-'}</div>
        <div class="label text-muted">总用户数</div>
      </div>
      <div class="stat-card">
        <div class="number text-primary">${s.stats.active_users || '-'}</div>
        <div class="label text-muted">活跃用户</div>
      </div>
      <div class="stat-card">
        <div class="number text-primary">${s.stats.total_plays || '-'}</div>
        <div class="label text-muted">播放记录</div>
      </div>
      <div class="stat-card">
        <div class="number text-primary">${s.stats.today_plays || '-'}</div>
        <div class="label text-muted">今日播放</div>
      </div>
    `;
  });
}
