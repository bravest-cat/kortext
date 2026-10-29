/**
 * Kortext Dashboard - Subsystems Health, Telemetry, Collections, and MINJA Quarantine
 * Monitors Watcher, Cache, SCIP, 5-Tier Memory Grid, and Next-Gen Interceptor Hooks.
 */

import { state } from './state.js';
import * as api from './api.js';
import { toast, escapeHtml } from './ui.js';

export async function loadHealth() {
  try {
    const data = await api.getStatus();
    if (data.services) updateQuickPills(data.services);
  } catch (err) {
    console.warn('Health check error:', err);
  }

  loadTelemetry();

  try {
    const wData = await api.getWatcherStatus();
    if (wData.status === 'ok' && wData.watcher) updateWatcherUI(wData.watcher);
  } catch (err) {
    console.warn('Watcher status error:', err);
  }

  try {
    const cData = await api.getCacheStats();
    if (cData.status === 'ok') updateCacheUI(cData);
  } catch (err) {
    console.warn('Cache stats error:', err);
  }

  if (window.lucide) window.lucide.createIcons();
}

export function updateQuickPills(services) {
  services.forEach(s => {
    const nameLower = s.name.toLowerCase();
    // Only match core database engines for quick pills, ignoring optional browser/dashboard UIs
    if (nameLower.includes('browser') && nameLower.includes('falkordb')) return;
    const id = nameLower.includes('qdrant') ? 'pill-qdrant'
             : (nameLower.includes('falkordb') && !nameLower.includes('browser')) ? 'pill-falkordb'
             : (nameLower.includes('omlx') || nameLower.includes('inference') || nameLower.includes('embed')) ? 'pill-inference' : null;
    if (id) {
      const el = document.getElementById(id);
      if (el) {
        const isEmbed = id === 'pill-inference';
        const isHealthy = s.healthy || isEmbed;
        const label = isEmbed
          ? (s.healthy ? 'Inference: Active' : 'Embed: FastEmbed')
          : escapeHtml(s.name.split(' ')[0]);

        el.className = isHealthy
          ? 'px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-400 border border-emerald-800/50 flex items-center gap-1.5'
          : 'px-2 py-0.5 rounded bg-rose-950/80 text-rose-400 border border-rose-800/50 flex items-center gap-1.5';
        el.innerHTML = `<span class="w-1.5 h-1.5 rounded-full ${isHealthy ? 'bg-emerald-400' : 'bg-rose-400'}"></span> ${label}`;
      }
    }
  });
}

export function updateWatcherUI(w) {
  const isRunning = w.running && w.alive !== false;
  const pill = document.getElementById('pill-watcher');
  if (pill) {
    pill.className = isRunning
      ? 'px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-400 border border-emerald-800/50 flex items-center gap-1.5'
      : 'px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700 flex items-center gap-1.5';
    pill.innerHTML = `<span class="w-1.5 h-1.5 rounded-full ${isRunning ? 'bg-emerald-400' : 'bg-slate-400'}"></span> Watcher: ${isRunning ? 'ON' : 'OFF'}`;
  }

  const badge = document.getElementById('watcher-status-badge');
  if (badge) {
    badge.className = isRunning
      ? 'text-[10px] font-mono px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 font-bold'
      : 'text-[10px] font-mono px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 border border-slate-700';
    badge.textContent = isRunning ? 'RUNNING' : 'STOPPED';
  }

  if (document.getElementById('watcher-root')) document.getElementById('watcher-root').textContent = w.root || '-';
  if (document.getElementById('watcher-debounce')) document.getElementById('watcher-debounce').textContent = (w.debounce_sec || 2.0) + 's';
  if (document.getElementById('watcher-processed')) document.getElementById('watcher-processed').textContent = (w.total_processed || 0).toLocaleString();
  if (document.getElementById('watcher-pending')) document.getElementById('watcher-pending').textContent = (w.pending_count || 0).toLocaleString();
  if (document.getElementById('watcher-last-file')) {
    const lf = w.last_synced_file ? w.last_synced_file.split('/').slice(-2).join('/') : '-';
    document.getElementById('watcher-last-file').textContent = lf;
  }
}

export function updateCacheUI(c) {
  const cc = c.chunk_cache || {};
  if (document.getElementById('cache-chunks-count')) {
    document.getElementById('cache-chunks-count').textContent = (cc.total_cached_chunks || 0).toLocaleString();
  }
  if (document.getElementById('cache-size-mb')) {
    document.getElementById('cache-size-mb').textContent = `${cc.storage_size_mb || 0} MB`;
  }
  if (document.getElementById('cache-indexed-files')) {
    document.getElementById('cache-indexed-files').textContent = (c.total_indexed_files || 0).toLocaleString();
  }
}

export async function clearChunkCache() {
  if (!confirm('Are you sure you want to clear the Chunk Embedding Cache? Sub-second incremental caching will reset.')) return;
  try {
    const data = await api.clearCache();
    if (data.status === 'ok') {
      toast(`✅ Purged ${data.purged_chunks} cached chunk vectors`, 'success');
      loadHealth();
    } else {
      toast(`Error clearing cache: ${data.message}`, 'error');
    }
  } catch (err) {
    toast('Cache clear failed: ' + err.message, 'error');
  }
}

export async function triggerScipIngest() {
  const btn = document.getElementById('btn-scip-ingest');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="animate-spin mr-1">⏳</span> Ingesting...';
  }
  try {
    const data = await api.triggerScip();
    if (data.status === 'ok') {
      toast(`✅ ${data.message}! Type edges populated.`, 'success');
    } else {
      toast(`ℹ️ SCIP Status: ${data.message}`, 'info');
    }
  } catch (err) {
    toast('SCIP ingestion error: ' + err.message, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<i data-lucide="play" class="w-3 h-3"></i> Ingest / Run SCIP';
      if (window.lucide) window.lucide.createIcons();
    }
  }
}

export async function loadTelemetry() {
  try {
    const data = await api.getTelemetry();
    if (data.status === 'ok' && data.telemetry) {
      const t = data.telemetry;
      const verEl = document.getElementById('kortext-version');
      if (verEl && t.version) verEl.innerText = `v${t.version}`;
      const rssEl = document.getElementById('hud-rss');
      const latEl = document.getElementById('hud-lat');
      const invEl = document.getElementById('hud-invariants');
      if (rssEl) rssEl.innerText = `${t.substrate_ram_rss_mb} MB RSS`;
      if (latEl) latEl.innerText = `${t.falkordb_latency_ms} ms`;
      if (invEl && t.active_invariants !== undefined) {
        invEl.innerText = `${t.active_invariants} Verified`;
      }
      const ctxEl = document.getElementById('hud-context-window');
      if (ctxEl && t.context_window) {
        const cw = t.context_window;
        const pct = cw.used_percentage || 0;
        const cPct = cw.compaction_budget_pct || 0;
        ctxEl.innerText = `${pct}% (${cPct}% of 25% ceil)`;
      }

      // Update 5-Tier Memory Grid UI if present
      renderMemoryTiersUI(t.memory_tiers);
      renderHooksStatusUI(t.nextgen_hooks);
      loadQuarantine();
      loadContextTelemetry();
      loadGroundedInvariants();
    }
  } catch (err) {
    console.warn('Telemetry load error:', err);
  }
}

function renderMemoryTiersUI(tiers) {
  const container = document.getElementById('memory-tiers-grid');
  if (!container || !tiers) return;
  const tierKeys = Object.keys(tiers);
  container.innerHTML = tierKeys.map(k => {
    const item = tiers[k];
    const statusText = (item.status || 'Active').toUpperCase();
    const isDegraded = statusText === 'DEGRADED' || statusText === 'OFFLINE';
    const badgeStyle = isDegraded
      ? 'bg-rose-500/10 text-rose-400 border-rose-500/30'
      : 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';

    return `
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-3.5 space-y-1.5 font-mono text-xs hover:border-slate-700 transition">
        <div class="flex items-center justify-between">
          <span class="text-indigo-400 font-bold uppercase text-[11px]">${k.replace('_', ' ')}</span>
          <span class="px-1.5 py-0.5 rounded text-[10px] border ${badgeStyle}">${escapeHtml(statusText)}</span>
        </div>
        <div class="font-semibold text-white">${escapeHtml(item.name || '')}</div>
        ${item.metrics ? `<div class="text-[11px] text-emerald-300 font-semibold tracking-wide">${escapeHtml(item.metrics)}</div>` : ''}
        <div class="text-[11px] text-slate-400">${escapeHtml(item.embedder || item.type || '')}</div>
      </div>
    `;
  }).join('');
}

function renderHooksStatusUI(hooks) {
  const container = document.getElementById('hooks-status-list');
  if (!container || !hooks) return;
  container.innerHTML = hooks.map(h => {
    const name = typeof h === 'string' ? h : h.name;
    const stage = typeof h === 'object' && h.pipeline ? h.pipeline.replace('_', ' ') : 'guard';
    const desc = typeof h === 'object' && h.description ? h.description : '';
    return `
      <span class="px-2.5 py-1.5 rounded-lg bg-slate-900 border border-slate-800 text-slate-300 font-mono text-xs flex items-center gap-2 hover:border-slate-700 transition" title="${escapeHtml(desc)}">
        <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(16,185,129,0.5)]"></span>
        <span class="text-slate-200 font-semibold">${escapeHtml(name)}</span>
        <span class="text-[9px] uppercase px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 font-bold">${escapeHtml(stage)}</span>
      </span>
    `;
  }).join('');
}

// Collections Manager
export async function loadCollections() {
  const container = document.getElementById('collections-grid');
  if (container) container.innerHTML = '<div class="text-slate-400 py-4 col-span-3 text-center text-xs font-mono">Fetching collections...</div>';
  try {
    const data = await api.getCollections();
    if (data.status !== 'ok' || !data.collections) {
      if (container) container.innerHTML = `<div class="text-rose-400 col-span-3 text-xs font-mono">Failed: ${data.message || 'Error'}</div>`;
      return;
    }
    if (container) {
      if (data.collections.length === 0) {
        container.innerHTML = '<div class="text-slate-400 py-4 col-span-3 text-center text-xs font-mono">No active collections found.</div>';
        return;
      }
      container.innerHTML = data.collections.map(c => {
        const name = typeof c === 'string' ? c : c.name;
        const pts = (c && c.points_count !== undefined) ? c.points_count.toLocaleString() : 'N/A';
        const dim = (c && c.vectors_size !== undefined) ? `${c.vectors_size} dims` : '4096 dims';
        return `
          <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-2 font-mono text-xs hover:border-indigo-500/40 transition">
            <div class="flex items-center justify-between">
              <span class="font-bold text-white text-sm truncate max-w-[180px]">${escapeHtml(name)}</span>
              <span class="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 text-[10px]">ACTIVE</span>
            </div>
            <div class="flex justify-between text-slate-400 text-[11px]">
              <span>Vectors:</span> <span class="text-white font-semibold">${pts} points</span>
            </div>
            <div class="flex justify-between text-slate-400 text-[11px]">
              <span>Dimensions:</span> <span class="text-slate-300">${dim}</span>
            </div>
          </div>
        `;
      }).join('');
    }
  } catch (err) {
    if (container) container.innerHTML = `<div class="text-rose-400 col-span-3 text-xs font-mono">Error: ${err.message}</div>`;
  }
}

export async function createNewCollection() {
  const input = document.getElementById('new-collection-name');
  const name = input ? input.value.trim() : '';
  if (!name) {
    toast('Please enter a collection name', 'warning');
    return;
  }
  try {
    const data = await api.createCollection(name);
    if (data.status === 'ok') {
      toast(`Collection '${name}' created successfully`, 'success');
      if (input) input.value = '';
      loadCollections();
    } else {
      toast(`Creation failed: ${data.message || 'error'}`, 'error');
    }
  } catch (err) {
    toast('Create collection error: ' + err.message, 'error');
  }
}

// MINJA Quarantine Forensics
export async function loadQuarantine() {
  const container = document.getElementById('quarantine-list');
  const countLabel = document.getElementById('quarantine-count-label');
  try {
    const data = await api.getQuarantine();
    if (data.status !== 'ok') return;
    const count = data.quarantine_count || 0;
    if (countLabel) countLabel.innerText = `${count} Violations Detected`;

    if (!container) return;
    if (!data.items || data.items.length === 0) {
      container.innerHTML = '<div class="text-slate-400 py-12 text-center text-xs font-mono">No adversarial violations detected. Causal memory is clean.</div>';
      return;
    }

    container.innerHTML = data.items.map(item => `
      <div class="bg-slate-950 border border-rose-500/20 rounded-xl p-3.5 space-y-2 font-mono text-xs">
        <div class="flex items-center justify-between">
          <span class="text-rose-400 font-bold">${escapeHtml(item.reason || 'Security Violation')}</span>
          <span class="text-slate-400 text-[10px]">${escapeHtml(item.timestamp || '')}</span>
        </div>
        <div class="text-slate-300 bg-slate-900 p-2.5 rounded-lg border border-slate-800">
          ${escapeHtml(item.fact || item.subject || '')}
        </div>
      </div>
    `).join('');
  } catch (err) {
    console.warn('Quarantine load error:', err);
  }
}

export async function clearQuarantine() {
  if (!confirm('Are you sure you want to clear the quarantine log? All quarantined adversarial payloads will be purged.')) return;
  try {
    const data = await api.clearQuarantine();
    if (data.status === 'ok') {
      toast(`Quarantine cleared (${data.cleared_count || 0} purged)`, 'success');
      loadQuarantine();
    } else {
      toast('Failed to clear quarantine: ' + (data.message || 'error'), 'error');
    }
  } catch (err) {
    toast('Clear quarantine error: ' + err.message, 'error');
  }
}

export function refreshAll() {
  loadHealth();
  if (state.activeTab === 'graph' && window.loadGraph) window.loadGraph();
  if (state.activeTab === 'journal' && window.loadJournal) window.loadJournal();
  if (state.activeTab === 'collections' && window.loadCollections) window.loadCollections();
  toast('Refreshed all active telemetry', 'info');
}

// Live Session Context & Token Burn Telemetry (25% Compaction Window Monitor)
export async function loadContextTelemetry() {
  const barEl = document.getElementById("ctx-compaction-bar");
  const usedEl = document.getElementById("ctx-used-tokens");
  const ceilEl = document.getElementById("ctx-compaction-pct");
  const savedEl = document.getElementById("ctx-tokens-saved");
  const batonsEl = document.getElementById("ctx-handoff-batons-list");

  try {
    const data = await api.getContextTelemetry();
    if (data.status !== "ok") return;
    const cw = data.context_window || {};
    const usedTokens = cw.total_input_tokens || 0;
    const winSize = cw.context_window_size || 1000000;
    const compPct = cw.compaction_budget_pct || 0;

    if (usedEl) usedEl.textContent = `${usedTokens.toLocaleString()} / ${(winSize / 4).toLocaleString()} tok (25% ceil)`;
    if (ceilEl) ceilEl.textContent = `${compPct}% of Compaction Ceiling`;
    if (savedEl) savedEl.textContent = `~${(data.estimated_tokens_saved || 0).toLocaleString()} tokens saved (${data.seek_symbol_calls || 0} AST seeks)`;

    if (barEl) {
      const clamped = Math.min(100, Math.max(2, compPct));
      barEl.style.width = `${clamped}%`;
      barEl.className = compPct >= 88
        ? "h-2 rounded-full bg-rose-500 transition-all"
        : (compPct >= 72 ? "h-2 rounded-full bg-amber-400 transition-all" : "h-2 rounded-full bg-emerald-400 transition-all");
    }

    if (batonsEl) {
      const batons = data.handoff_batons || [];
      if (batons.length === 0) {
        batonsEl.innerHTML = "<div class=\"text-slate-400 text-xs font-mono py-2\">No pre-compaction handoff batons recorded yet.</div>";
      } else {
        batonsEl.innerHTML = batons.slice(0, 5).map(b => `
          <div class="p-2 rounded-lg bg-slate-950 border border-slate-800 flex items-center justify-between text-xs font-mono">
            <div class="truncate max-w-[260px]">
              <span class="text-indigo-400 font-bold">${escapeHtml(b.task_id || b.filename)}</span>
              <span class="text-slate-400 ml-2">${escapeHtml(b.current_scope || "")}</span>
            </div>
            <span class="text-[10px] text-slate-400">${escapeHtml(b.target_agent || "orchestrator")}</span>
          </div>
        `).join("");
      }
    }
  } catch (err) {
    console.warn("Context telemetry load error:", err);
  }
}

// Grounded invariants audit
export async function loadGroundedInvariants() {
  const invContainer = document.getElementById("grounded-invariants-list");
  const invBadge = document.getElementById("invariants-drift-badge");

  try {
    const invData = await api.getGroundedInvariants();

    if (invData.status === "ok") {
      const audit = invData.drift_audit || {};
      const drifted = audit.drifted_count || 0;
      if (invBadge) {
        invBadge.textContent = drifted > 0 ? `${drifted} Drifted` : "0 Drifted (Verified)";
        invBadge.className = drifted > 0
          ? "text-[10px] font-mono px-2 py-0.5 rounded-full bg-rose-500/10 text-rose-400 border border-rose-500/30 font-bold"
          : "text-[10px] font-mono px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 font-bold";
      }
      if (invContainer) {
        const records = invData.grounded_invariants || [];
        if (records.length === 0) {
          invContainer.innerHTML = "<div class=\"text-slate-400 text-xs font-mono py-4 text-center\">All AST-grounded invariants are in sync with live Tree-sitter body hashes.</div>";
        } else {
          invContainer.innerHTML = records.slice(0, 10).map(r => `
            <div class="p-3 rounded-xl bg-slate-950 border border-slate-800 space-y-1 font-mono text-xs">
              <div class="flex items-center justify-between">
                <span class="font-bold text-white">${escapeHtml(r.symbol_name || r.domain || "Invariant")}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">${escapeHtml(r.status || "verified")}</span>
              </div>
              <div class="text-slate-300 text-[11px]">${escapeHtml(r.rule || "")}</div>
              <div class="text-[10px] text-slate-400 truncate">AST SHA-256: ${escapeHtml((r.ast_hash || "").slice(0, 16))} • ${escapeHtml(r.file_path || "")}</div>
            </div>
          `).join("");
        }
      }
    }
  } catch (err) {
    console.warn("Invariants load error:", err);
  }
}
