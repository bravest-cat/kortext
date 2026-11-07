/**
 * Kortext Dashboard - Overview Module
 * Landing tab displaying real-time substrate health, re-index queue, project freshness, and token savings.
 */

import * as api from './api.js';
import { escapeHtml, formatDate, renderErrorState, renderEmptyState } from './ui.js';

export async function loadOverview() {
  const container = document.getElementById('tab-content-overview');
  if (!container) return;

  try {
    const data = await api.getOverview();
    if (data.status !== 'ok') {
      renderErrorState(container, data.message || 'Failed to load overview data', loadOverview);
      return;
    }

    renderSubstrates(data);
    renderReindexQueue(data.reindex || {});
    renderProjectFreshness(data.projects || []);
    loadSavings();
  } catch (err) {
    renderErrorState(container, err.message, loadOverview);
  }
}

function renderSubstrates(data) {
  // 1. Daemon
  const daemonEl = document.getElementById('overview-daemon-status');
  if (daemonEl) {
    const isRunning = data.daemon && data.daemon.status === 'RUNNING';
    daemonEl.innerHTML = `
      <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-mono font-semibold ${
        isRunning ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30' : 'bg-slate-800 text-slate-400 border border-slate-700'
      }">
        <span class="w-2 h-2 rounded-full ${isRunning ? 'bg-emerald-400' : 'bg-slate-400'}"></span>
        ${isRunning ? 'RUNNING' : 'STOPPED'}
      </span>
    `;
  }

  // 2. FalkorDB
  const falkorEl = document.getElementById('overview-falkordb-status');
  if (falkorEl) {
    const isHealthy = data.falkordb && data.falkordb.healthy;
    const port = data.falkordb ? data.falkordb.port : 'unknown';
    falkorEl.innerHTML = `
      <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-mono font-semibold ${
        isHealthy ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30' : 'bg-rose-500/10 text-rose-400 border border-rose-500/30'
      }">
        <span class="w-2 h-2 rounded-full ${isHealthy ? 'bg-emerald-400' : 'bg-rose-400'}"></span>
        ${isHealthy ? 'ONLINE' : 'OFFLINE'} (:${escapeHtml(String(port))})
      </span>
    `;
  }

  // 3. Qdrant
  const qdrantEl = document.getElementById('overview-qdrant-status');
  if (qdrantEl) {
    const isHealthy = data.qdrant && data.qdrant.healthy;
    qdrantEl.innerHTML = `
      <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-mono font-semibold ${
        isHealthy ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30' : 'bg-rose-500/10 text-rose-400 border border-rose-500/30'
      }">
        <span class="w-2 h-2 rounded-full ${isHealthy ? 'bg-emerald-400' : 'bg-rose-400'}"></span>
        ${isHealthy ? 'ONLINE' : 'OFFLINE'}
      </span>
    `;
  }
}

function renderReindexQueue(reindex) {
  const summaryEl = document.getElementById('overview-reindex-summary');
  const failuresEl = document.getElementById('overview-reindex-failures');

  const pending = typeof reindex.pending === 'number' ? reindex.pending : 'unknown';
  const failed = typeof reindex.failed === 'number' ? reindex.failed : 'unknown';

  if (summaryEl) {
    summaryEl.textContent = `${pending} pending, ${failed} failed`;
  }

  if (failuresEl) {
    const recent = reindex.recent_failures || [];
    if (recent.length === 0) {
      failuresEl.innerHTML = '<div class="text-xs text-slate-400 font-mono py-2">No re-index failures recorded.</div>';
    } else {
      failuresEl.innerHTML = recent.map(f => `
        <div class="p-3 bg-slate-950 border border-rose-500/30 rounded-lg space-y-1 font-mono text-xs">
          <div class="flex items-center justify-between text-slate-300">
            <span class="font-bold text-rose-400 truncate max-w-[280px]" title="${escapeHtml(f.path || 'unknown')}">${escapeHtml(f.path || 'unknown')}</span>
            <span class="text-[10px] text-slate-400">Attempts: ${f.attempts !== undefined ? escapeHtml(String(f.attempts)) : 'unknown'}</span>
          </div>
          <div class="text-[11px] text-rose-300/90 break-words">${escapeHtml(f.error || 'Unknown error')}</div>
        </div>
      `).join('');
    }
  }
}

function renderProjectFreshness(projects) {
  const container = document.getElementById('overview-projects-list');
  if (!container) return;

  if (!projects || projects.length === 0) {
    renderEmptyState(container, 'nothing indexed yet, run kortext init');
    return;
  }

  container.innerHTML = `
    <div class="overflow-x-auto min-w-0">
      <table class="w-full text-left text-xs font-mono">
        <thead class="text-slate-400 border-b border-slate-800">
          <tr>
            <th class="py-2.5 px-3">Project</th>
            <th class="py-2.5 px-3">Last Indexed</th>
            <th class="py-2.5 px-3">Files</th>
            <th class="py-2.5 px-3">Symbols</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-800/60">
          ${projects.map(p => {
            const lastIndexed = p.last_indexed ? formatDate(p.last_indexed) : 'unknown';
            const files = p.files !== null && p.files !== undefined ? p.files.toLocaleString() : 'unknown';
            const symbols = p.symbols !== null && p.symbols !== undefined ? p.symbols.toLocaleString() : 'unknown';
            return `
              <tr class="hover:bg-slate-900/50 transition">
                <td class="py-2.5 px-3 font-semibold text-white">${escapeHtml(p.project || 'unknown')}</td>
                <td class="py-2.5 px-3 text-slate-400">${escapeHtml(lastIndexed)}</td>
                <td class="py-2.5 px-3 text-slate-300">${escapeHtml(files)}</td>
                <td class="py-2.5 px-3 text-indigo-400 font-semibold">${escapeHtml(symbols)}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

async function loadSavings() {
  const panel = document.getElementById('overview-savings-panel');
  if (!panel) return;

  try {
    const data = await api.getSavings();
    if (!data || data.status === 'error') {
      panel.classList.add('hidden');
      return;
    }

    panel.classList.remove('hidden');
    const estEl = document.getElementById('overview-savings-estimate');
    const countsEl = document.getElementById('overview-savings-counts');

    if (estEl) {
      const est = data.estimate !== undefined && data.estimate !== null ? data.estimate.toLocaleString() : 'unknown';
      estEl.textContent = `${est} tokens`;
    }

    if (countsEl) {
      const counts = data.counts || {};
      const pairs = Object.entries(counts);
      if (pairs.length > 0) {
        countsEl.innerHTML = pairs.map(([k, v]) => `
          <div><span class="text-slate-400">${escapeHtml(k)}:</span> <span class="text-white font-semibold">${escapeHtml(String(v))}</span></div>
        `).join('');
      } else {
        countsEl.innerHTML = '';
      }
    }
  } catch (_) {
    // Hide panel if 404 or any other error
    panel.classList.add('hidden');
  }
}
