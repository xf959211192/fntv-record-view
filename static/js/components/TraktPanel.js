import { state } from '../state.js';
import { api } from '../api.js';
import { escapeHtml } from '../utils.js';

let traktAuthExpireTimer = null;

export function initTraktPanel(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;

  state.subscribe((s) => {
    renderTraktPanel(container, s);
  });
}

function renderTraktPanel(container, s) {
  const status = s.traktStatus || {};
  const db = s.traktDashboard || { summary: {}, failed_queue: [], sync_states: [] };

  const badgeClass = !status.configured
    ? 'badge-info'
    : status.connected
      ? 'badge-success'
      : s.isTraktSubmitting
        ? 'badge-info'
        : 'badge-outline';

  const statusLabel = !status.configured ? '未配置' : status.connected ? '已连接' : '未连接';
  const authMessageHtml = !status.configured
    ? '后端尚未配置 TRAKT_CLIENT_ID / TRAKT_CLIENT_SECRET。'
    : status.connected
      ? 'Trakt 已连接，可以执行同步或修改设置。'
      : '请先连接 Trakt 并完成授权。';

  const summary = db.summary || {};
  const onlyWatched = localStorage.getItem('trakt_only_watched') !== 'false';
  const watchedThreshold = localStorage.getItem('trakt_watched_threshold') || 90;

  container.innerHTML = `
    <div class="trakt-panel">
      <div class="flex gap-3 justify-between items-center mb-6">
        <h3 class="text-primary font-bold text-xl">Trakt 运营面板</h3>
      </div>

      <div class="flex gap-3 items-center mb-6 p-4 surface">
        <span class="badge ${badgeClass}">${statusLabel}</span>
        <span class="text-muted text-sm">${authMessageHtml}</span>
      </div>

      <div class="flex gap-4 mb-6">
        <button id="btn-trakt-connect" class="btn btn-secondary" ${status.connected ? 'disabled' : ''}>连接 Trakt</button>
        <button id="btn-trakt-preview" class="btn btn-secondary" ${!status.connected || s.isTraktSubmitting ? 'disabled' : ''}>预览同步</button>
        <button id="btn-trakt-sync" class="btn btn-primary" ${!status.connected || s.isTraktSubmitting ? 'disabled' : ''}>
          ${s.isTraktSubmitting ? '同步中...' : '启动同步'}
        </button>
      </div>

      <h4 class="mb-3 font-bold">同步预设</h4>
      <div class="surface p-6 mb-6">
        <div class="grid gap-4" style="grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));">
          <div class="flex-col gap-2 flex">
            <label class="flex items-center gap-2 cursor-pointer font-medium text-sm">
              <input type="checkbox" id="trakt-only-watched" ${onlyWatched ? 'checked' : ''}>
              仅同步已看完或达到阈值的记录
            </label>
            <label class="flex items-center gap-2">
              <span class="text-sm">视作已播放阈值</span>
              <input type="number" id="trakt-watched-threshold" value="${escapeHtml(String(watchedThreshold))}" min="1" max="100" class="input-base" style="width: 80px;">
              <span class="text-sm">%</span>
            </label>
          </div>
          <div class="flex-col gap-2 flex">
            <label class="font-medium text-sm">定时同步范围设置</label>
            <div class="flex items-center gap-2">
              <select id="trakt-auto-sync-user" class="input-base flex-1">
                <option value="">所有用户</option>
                ${(s.users || []).map((u) => `
                  <option value="${escapeHtml(u.guid)}" ${status.auto_sync_user_guid === u.guid ? 'selected' : ''}>
                    ${escapeHtml(u.username)}${u.is_admin ? ' (管理员)' : ''}
                  </option>
                `).join('')}
              </select>
              <button id="btn-trakt-save-auto" class="btn btn-secondary">保存</button>
            </div>
            <div class="text-sm text-muted mt-1">当前范围: ${escapeHtml(status.auto_sync_user_display || '所有用户')}</div>
          </div>
        </div>
      </div>

      <h4 class="mb-3 font-bold">数据概览</h4>
      <div class="ops-grid mb-6">
        <div class="ops-card">
          <div class="number text-warning font-bold text-2xl">${summary.failed_pending || 0}</div>
          <div class="label text-muted text-sm">待处理失败</div>
        </div>
        <div class="ops-card">
          <div class="number text-primary font-bold text-2xl">${summary.review_required || 0}</div>
          <div class="label text-muted text-sm">需人工复核</div>
        </div>
        <div class="ops-card">
          <div class="number text-success font-bold text-2xl">${summary.matched || 0}</div>
          <div class="label text-muted text-sm">匹配成功</div>
        </div>
        <div class="ops-card">
          <div class="number text-danger font-bold text-2xl">${summary.failed || 0}</div>
          <div class="label text-muted text-sm">严重失败</div>
        </div>
      </div>

      ${renderLastSyncDetails(status.last_sync)}

      <h4 class="mb-3 font-bold text-danger">同步失败队列</h4>
      <div class="flex-col gap-3 flex mb-6">
        ${renderFailedQueue(db.failed_queue)}
      </div>

      <h4 class="mb-3 font-bold text-info">历史同步状态</h4>
      <div class="flex-col gap-3 flex mb-6">
        ${renderSyncStates(db.sync_states)}
      </div>
    </div>
  `;

  bindTraktPanelEvents();
}

function renderLastSyncDetails(lastSync) {
  if (!lastSync || !lastSync.synced_at_display) {
    return '';
  }

  const traktResp = lastSync.trakt_response || {};
  return `
    <h4 class="mb-3 font-bold">最近一次同步: ${escapeHtml(lastSync.synced_at_display)}</h4>
    <div class="surface p-4 mb-6 shadow-glow grid gap-2 text-sm" style="grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));">
      <div>读取记录: ${lastSync.total_records || 0} / 准备: ${lastSync.prepared || 0}</div>
      <div>电影: ${lastSync.movies || 0} / 剧集: ${lastSync.episodes || 0}</div>
      <div class="text-success">Trakt 新增 电影: ${traktResp.added?.movies || 0} / 剧集: ${traktResp.added?.episodes || 0}</div>
      <div class="text-info">Trakt 更新 电影: ${traktResp.updated?.movies || 0} / 剧集: ${traktResp.updated?.episodes || 0}</div>
    </div>
  `;
}

function renderFailedQueue(queue) {
  if (!queue || queue.length === 0) {
    return '<div class="ops-empty">当前没有待处理失败项。</div>';
  }

  return queue.map((item) => {
    const payload = item.candidate_payload || {};
    const title = payload.display_title || payload.title || item.item_guid;
    const candidateTitle = payload.candidate_title || '';
    const confidence = payload.confidence || '';
    const matchNotes = Array.isArray(payload.match_notes) ? payload.match_notes.filter(Boolean).join(' / ') : '';
    const localIds = formatLocalIdComparison(payload);
    const candidateIds = formatCandidateIdComparison(payload);
    const canSyncCandidate = item.reason === 'low_confidence' && payload.candidate_ids && payload.candidate_ids.trakt;

    return `
      <div class="surface p-5 flex justify-between items-center" style="border-left: 4px solid var(--warning);">
        <div>
          <div class="font-bold text-primary mb-2">${escapeHtml(title)}</div>
          <div class="text-sm text-muted">原因: ${escapeHtml(item.reason)} | 状态: ${escapeHtml(item.status)}</div>
          ${candidateTitle ? `<div class="text-sm text-muted mt-1">匹配项: ${escapeHtml(candidateTitle)}</div>` : ''}
          ${confidence ? `<div class="text-sm text-muted mt-1">置信度: ${escapeHtml(confidence)}</div>` : ''}
          ${matchNotes ? `<div class="text-sm text-muted mt-1">校验说明: ${escapeHtml(matchNotes)}</div>` : ''}
          ${localIds ? `<div class="text-sm text-muted mt-1">本地 ID: ${escapeHtml(localIds)}</div>` : ''}
          ${candidateIds ? `<div class="text-sm text-muted mt-1">候选匹配 ID: ${escapeHtml(candidateIds)}</div>` : ''}
        </div>
        <div class="flex gap-2 flex-wrap shrink-0 justify-end" style="max-width:280px;">
          ${canSyncCandidate ? `<button class="btn btn-primary btn-sm btn-sync-candidate" data-user="${escapeHtml(item.user_guid)}" data-item="${escapeHtml(item.item_guid)}">按候选同步</button>` : ''}
          <button class="btn btn-secondary btn-sm btn-rematch" data-user="${escapeHtml(item.user_guid)}" data-item="${escapeHtml(item.item_guid)}">重试查询</button>
          <button class="btn btn-secondary btn-sm btn-resolve" data-user="${escapeHtml(item.user_guid)}" data-item="${escapeHtml(item.item_guid)}" data-type="${escapeHtml(payload.type || '')}">人为绑定 ID</button>
        </div>
      </div>
    `;
  }).join('');
}

function formatLocalIdComparison(payload) {
  const segments = [];
  const itemExternalIds = payload.item_external_ids || {};
  const episodeExternalIds = payload.episode_external_ids || {};

  if (payload.type === 'movie') {
    const localTmdb = payload.tmdb_id || itemExternalIds.tmdb;
    const localImdb = payload.imdb_id || itemExternalIds.imdb;
    if (localTmdb) segments.push(`tmdb=${localTmdb}`);
    if (localImdb) segments.push(`imdb=${localImdb}`);
    return segments.join(' | ');
  }

  if (payload.show_tmdb_id) segments.push(`show.tmdb=${payload.show_tmdb_id}`);
  if (payload.show_imdb_id) segments.push(`show.imdb=${payload.show_imdb_id}`);
  if (payload.episode_imdb_id) segments.push(`episode.imdb=${payload.episode_imdb_id}`);
  if (episodeExternalIds.tmdb) segments.push(`episode.tmdb=${episodeExternalIds.tmdb}`);
  if (episodeExternalIds.tvdb || episodeExternalIds.thetvdb) {
    segments.push(`episode.tvdb=${episodeExternalIds.tvdb || episodeExternalIds.thetvdb}`);
  }

  return segments.join(' | ');
}

function formatCandidateIdComparison(payload) {
  const segments = [];
  const candidateIds = payload.candidate_ids || {};

  if (payload.type === 'episode' && payload.candidate_show_id) {
    segments.push(`show.trakt=${payload.candidate_show_id}`);
  }
  if (candidateIds.trakt) segments.push(`trakt=${candidateIds.trakt}`);
  if (candidateIds.imdb) segments.push(`imdb=${candidateIds.imdb}`);
  if (candidateIds.tmdb) segments.push(`tmdb=${candidateIds.tmdb}`);
  if (candidateIds.tvdb) segments.push(`tvdb=${candidateIds.tvdb}`);

  return segments.join(' | ');
}

function renderSyncStates(states) {
  if (!states || states.length === 0) {
    return '<div class="ops-empty">当前没有同步状态记录。</div>';
  }

  return states.map((item) => {
    let color = 'var(--text-muted)';
    if (item.sync_status === 'failed') color = 'var(--danger)';
    else if (item.sync_status === 'matched' || item.sync_status === 'synced') color = 'var(--success)';

    return `
      <div class="surface p-4 flex justify-between items-center" style="border-left: 4px solid ${color};">
        <div>
          <div class="font-bold mb-1">${escapeHtml(item.title || item.item_guid || '未命名')}</div>
          <div class="text-sm text-muted flex gap-2">
            <span class="badge-outline">用户 ${escapeHtml(item.username || item.user_guid)}</span>
            <span class="badge-outline">状态 ${escapeHtml(item.sync_status)}</span>
            ${item.last_remote_id ? `<span class="badge-outline">ID: ${escapeHtml(item.last_remote_id)}</span>` : ''}
          </div>
          ${item.error_message ? `<div class="text-danger text-sm mt-2">${escapeHtml(item.error_message)}</div>` : ''}
        </div>
        <div class="text-xs text-muted text-right shrink-0">
          重试: ${item.retry_count || 0}<br>${escapeHtml(item.updated_at_display || '')}
        </div>
      </div>
    `;
  }).join('');
}

function startModalCountdown(expiresInMsgId, expiresAtMS) {
  const el = document.getElementById(expiresInMsgId);
  if (!el) return;
  if (traktAuthExpireTimer) clearInterval(traktAuthExpireTimer);

  traktAuthExpireTimer = setInterval(() => {
    const remainingMs = Math.max(0, expiresAtMS - Date.now());
    if (remainingMs <= 0) {
      el.innerText = '验证码已过期。';
      clearInterval(traktAuthExpireTimer);
      return;
    }
    const seconds = Math.floor(remainingMs / 1000);
    const mins = String(Math.floor(seconds / 60)).padStart(2, '0');
    const secs = String(seconds % 60).padStart(2, '0');
    el.innerText = `验证码剩余有效期: ${mins}:${secs}`;
  }, 1000);
}

function bindTraktPanelEvents() {
  const checkboxWatched = document.getElementById('trakt-only-watched');
  const inputThreshold = document.getElementById('trakt-watched-threshold');
  const connectBtn = document.getElementById('btn-trakt-connect');
  const syncBtn = document.getElementById('btn-trakt-sync');
  const previewBtn = document.getElementById('btn-trakt-preview');
  const saveAutoBtn = document.getElementById('btn-trakt-save-auto');

  if (checkboxWatched) {
    checkboxWatched.addEventListener('change', (e) => {
      localStorage.setItem('trakt_only_watched', String(e.target.checked));
    });
  }

  if (inputThreshold) {
    inputThreshold.addEventListener('change', (e) => {
      localStorage.setItem('trakt_watched_threshold', String(e.target.value));
      if (checkboxWatched) checkboxWatched.checked = true;
    });
  }

  if (saveAutoBtn) {
    saveAutoBtn.addEventListener('click', async () => {
      const userGuid = document.getElementById('trakt-auto-sync-user')?.value || '';
      try {
        await api.updateTraktSettings({ auto_sync_user_guid: userGuid });
        alert('定时同步范围保存成功');
        const traktStatus = await api.getTraktStatus();
        state.update({ traktStatus });
      } catch (err) {
        alert(`定时范围保存错误: ${err.message}`);
      }
    });
  }

  if (connectBtn) {
    connectBtn.addEventListener('click', async () => {
      try {
        state.update({ isTraktSubmitting: true });
        const res = await api.traktDeviceStart();

        document.getElementById('trakt-user-code').innerText = res.user_code;
        const verificationLink = document.getElementById('trakt-verification-link');
        verificationLink.href = res.verification_url;
        verificationLink.innerText = res.verification_url;
        startModalCountdown('trakt-modal-expire', Date.now() + ((res.expires_in || 600) * 1000));
        document.getElementById('trakt-modal').classList.add('open');

        let iters = 0;
        const interval = setInterval(async () => {
          iters += 1;
          try {
            const stat = await api.traktDeviceStatus(res.session_id);
            if (stat.status === 'authorized') {
              clearInterval(interval);
              document.getElementById('trakt-modal').classList.remove('open');
              alert('授权成功');
              state.update({ isTraktSubmitting: false });
              const traktStatus = await api.getTraktStatus();
              state.update({ traktStatus });
            } else if (stat.status === 'expired' || iters > 60) {
              clearInterval(interval);
              document.getElementById('trakt-modal').classList.remove('open');
              alert('授权已过期');
              state.update({ isTraktSubmitting: false });
            }
          } catch (_e) {
            // 轮询保持静默。
          }
        }, 5000);
      } catch (e) {
        alert(`发起授权失败: ${e.message}`);
        state.update({ isTraktSubmitting: false });
      }
    });
  }

  const performSync = async (dryRun) => {
    state.update({ isTraktSubmitting: true });
    try {
      const threshold = parseInt(document.getElementById('trakt-watched-threshold')?.value || '90', 10) || 90;
      const onlyWatchedValue = document.getElementById('trakt-only-watched')?.checked ?? true;
      const userSelectValue = document.getElementById('user-select')?.value || '';

      const res = await api.traktSync({
        user_guid: userSelectValue,
        dry_run: dryRun,
        only_watched: onlyWatchedValue,
        watched_threshold: threshold,
        limit: 200
      });

      let msg = dryRun ? '【预览同步结果】\n' : '【同步提交结果】\n';
      msg += res.message || '';
      if (res.prepared !== undefined) msg += `\n处理数目: ${res.prepared} 条`;
      if (res.skipped?.skipped_no_ids !== undefined) msg += `\n跳过 ID 未知条目: ${res.skipped.skipped_no_ids} 条`;
      if (dryRun && res.payload_preview) msg += `\n预览样例数: ${(res.payload_preview.episodes || []).length} 剧集`;

      alert(msg);
    } catch (e) {
      alert(`请求失败: ${e.message}`);
    } finally {
      state.update({ isTraktSubmitting: false });
      if (!dryRun) await refreshTraktDashboard();
    }
  };

  if (previewBtn) previewBtn.addEventListener('click', () => performSync(true));
  if (syncBtn) {
    syncBtn.addEventListener('click', async () => {
      if (!confirm('马上开始真实并批量推送到 Trakt，继续吗？')) return;
      await performSync(false);
    });
  }

  document.querySelectorAll('.btn-rematch').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      const user = e.currentTarget.dataset.user;
      const item = e.currentTarget.dataset.item;
      try {
        await api.rematchFailedQueue(user, item);
        alert('已重新发起服务端查询');
        await refreshTraktDashboard();
      } catch (err) {
        alert(`重新查询失败: ${err.message}`);
      }
    });
  });

  document.querySelectorAll('.btn-resolve').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      const user = e.currentTarget.dataset.user;
      const item = e.currentTarget.dataset.item;
      const type = e.currentTarget.dataset.type;
      const tid = prompt('请输入你要人为绑定的真实 Trakt ID（纯数字）:');
      if (!tid) return;

      try {
        await api.manualResolveQueue({
          user_guid: user,
          item_guid: item,
          trakt_id: tid,
          media_type: type,
          trakt_show_id: ''
        });
        alert('人为绑定已下发');
        await refreshTraktDashboard();
      } catch (err) {
        alert(`人为绑定故障: ${err.message}`);
      }
    });
  });

  document.querySelectorAll('.btn-sync-candidate').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      const user = e.currentTarget.dataset.user;
      const item = e.currentTarget.dataset.item;
      try {
        await api.syncFailedQueueCandidate(user, item);
        alert('已强制按当前候选匹配项同步');
        await refreshTraktDashboard();
      } catch (err) {
        alert(`候选覆盖绑定故障: ${err.message}`);
      }
    });
  });
}

async function refreshTraktDashboard() {
  const dash = await api.getTraktDashboard();
  state.update({ traktDashboard: dash });
}
