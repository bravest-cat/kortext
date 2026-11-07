/**
 * Kortext Dashboard - Memory Inbox Module
 * Inspect, filter, approve, edit, and supersede episodic memories and facts.
 * Security constraint: NEVER use innerHTML with memory text; always escape or set textContent.
 */

import * as api from './api.js';
import { state } from './state.js';
import {
  escapeHtml,
  formatDate,
  toast,
  openModal,
  closeModal,
  renderErrorState,
  renderEmptyState,
} from './ui.js';

let allMemories = [];
let editingMemoryId = null;
let editingMemoryKind = 'fact';

export async function loadMemory() {
  const container = document.getElementById('memory-list-container');
  if (!container) return;

  container.innerHTML = '<div class="text-slate-400 py-12 text-center text-xs font-mono">Loading memories...</div>';

  const proj = state.activeProject && state.activeProject !== 'all' ? state.activeProject : null;

  try {
    const data = await api.getMemories(proj, 200, 0);
    if (data.status !== 'ok') {
      renderErrorState(container, data.message || 'Failed to load memories', loadMemory);
      return;
    }

    allMemories = data.memories || [];
    if (allMemories.length === 0) {
      renderEmptyState(container, 'nothing indexed yet, run kortext init');
      return;
    }

    applyMemoryFilters();
  } catch (err) {
    renderErrorState(container, err.message, loadMemory);
  }
}

export function applyMemoryFilters() {
  const container = document.getElementById('memory-list-container');
  if (!container) return;

  const kindFilter = (document.getElementById('memory-filter-kind')?.value || 'all').toLowerCase();
  const staleOnly = document.getElementById('memory-filter-stale')?.checked || false;
  const unverifiedOnly = document.getElementById('memory-filter-unverified')?.checked || false;
  const searchQuery = (document.getElementById('memory-search-input')?.value || '').toLowerCase().trim();
  const activeProj = state.activeProject;

  let filtered = allMemories;

  // Project filter
  if (activeProj && activeProj !== 'all') {
    filtered = filtered.filter(m => (m.project || 'kortext').toLowerCase() === activeProj.toLowerCase());
  }

  // Kind filter
  if (kindFilter !== 'all') {
    filtered = filtered.filter(m => (m.kind || 'fact').toLowerCase() === kindFilter);
  }

  // Stale only filter
  if (staleOnly) {
    filtered = filtered.filter(m => Boolean(m.stale));
  }

  // Unverified only filter (exclude verified and user_approved)
  if (unverifiedOnly) {
    filtered = filtered.filter(m => {
      const t = (m.trust_level || '').toLowerCase();
      return t !== 'verified' && t !== 'user_approved';
    });
  }

  // Search query filter
  if (searchQuery) {
    filtered = filtered.filter(m => {
      const txt = (m.text || '').toLowerCase();
      const id = (m.id || '').toLowerCase();
      return txt.includes(searchQuery) || id.includes(searchQuery);
    });
  }

  renderMemoryList(filtered, container);
}

function renderMemoryList(items, container) {
  container.innerHTML = '';

  if (items.length === 0) {
    container.innerHTML = '<div class="text-slate-400 py-12 text-center text-xs font-mono">No memories match the active filters.</div>';
    return;
  }

  const fragment = document.createDocumentFragment();

  items.forEach(mem => {
    const card = document.createElement('div');
    card.className = 'bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3 hover:border-slate-700 transition font-mono text-xs';
    card.setAttribute('data-memory-id', mem.id || '');

    // Header metadata row
    const header = document.createElement('div');
    header.className = 'flex flex-wrap items-center justify-between gap-2 border-b border-slate-800/80 pb-2.5';

    const leftMeta = document.createElement('div');
    leftMeta.className = 'flex flex-wrap items-center gap-2';

    // Kind badge
    const kindBadge = document.createElement('span');
    const isEpisode = (mem.kind || '').toLowerCase() === 'episode';
    kindBadge.className = `px-2 py-0.5 rounded text-[10px] font-bold uppercase border ${
      isEpisode ? 'bg-purple-500/10 text-purple-300 border-purple-500/30' : 'bg-sky-500/10 text-sky-300 border-sky-500/30'
    }`;
    kindBadge.textContent = mem.kind || 'fact';
    leftMeta.appendChild(kindBadge);

    // ID badge
    const idBadge = document.createElement('span');
    idBadge.className = 'text-slate-400 text-[11px] font-semibold';
    idBadge.textContent = mem.id || '';
    leftMeta.appendChild(idBadge);

    // Trust badge
    const trustBadge = document.createElement('span');
    const trust = (mem.trust_level || 'speculative').toLowerCase();
    const trustStyles = {
      user_approved: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
      verified: 'bg-teal-500/15 text-teal-300 border-teal-500/40',
      tool_confirmed: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
      speculative: 'bg-slate-800 text-slate-400 border-slate-700',
      untrusted: 'bg-rose-500/15 text-rose-300 border-rose-500/40',
    };
    trustBadge.className = `px-2 py-0.5 rounded text-[10px] font-bold uppercase border ${trustStyles[trust] || trustStyles.speculative}`;
    trustBadge.textContent = trust;
    leftMeta.appendChild(trustBadge);

    // Project badge
    const projBadge = document.createElement('span');
    projBadge.className = 'text-slate-500 text-[10px]';
    projBadge.textContent = `[${mem.project || 'kortext'}]`;
    leftMeta.appendChild(projBadge);

    // Stale flag
    if (mem.stale) {
      const staleBadge = document.createElement('span');
      staleBadge.className = 'px-1.5 py-0.5 rounded text-[9px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40';
      staleBadge.textContent = 'STALE';
      if (mem.stale_symbols && mem.stale_symbols.length) {
        staleBadge.title = `Stale symbols: ${mem.stale_symbols.join(', ')}`;
      }
      leftMeta.appendChild(staleBadge);
    }

    // Superseded flag
    if (mem.superseded) {
      const superBadge = document.createElement('span');
      superBadge.className = 'px-1.5 py-0.5 rounded text-[9px] font-bold bg-slate-800 text-slate-400 border border-slate-700';
      superBadge.textContent = 'SUPERSEDED';
      leftMeta.appendChild(superBadge);
    }

    header.appendChild(leftMeta);

    // Date
    const dateEl = document.createElement('div');
    dateEl.className = 'text-slate-400 text-[10px]';
    dateEl.textContent = mem.created_at ? formatDate(mem.created_at) : '';
    header.appendChild(dateEl);

    card.appendChild(header);

    // Memory Text Content: MUST use textContent to prevent DOM XSS
    const textEl = document.createElement('div');
    textEl.className = 'memory-text-content p-3 bg-slate-950 border border-slate-800/80 rounded-lg text-slate-200 font-sans text-xs whitespace-pre-wrap break-words leading-relaxed';
    textEl.textContent = mem.text || ''; // STRICTLY textContent!
    card.appendChild(textEl);

    // Action button bar
    const actions = document.createElement('div');
    actions.className = 'flex items-center justify-end gap-2 pt-1 border-t border-slate-800/60';

    // 1. Approve Button
    const isApproved = (mem.trust_level || '').toLowerCase() === 'user_approved';
    const approveBtn = document.createElement('button');
    approveBtn.className = `px-2.5 py-1 rounded text-xs font-mono font-medium flex items-center gap-1 transition ${
      isApproved
        ? 'bg-slate-800 text-slate-400 cursor-not-allowed opacity-60'
        : 'bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-300 border border-emerald-500/40 cursor-pointer'
    }`;
    approveBtn.innerHTML = '<i data-lucide="check" class="w-3.5 h-3.5"></i> ' + (isApproved ? 'Approved' : 'Approve');
    approveBtn.disabled = isApproved;
    if (!isApproved) {
      approveBtn.addEventListener('click', () => handleApprove(mem.id, mem.kind || 'fact'));
    }
    actions.appendChild(approveBtn);

    // 2. Edit Button
    const editBtn = document.createElement('button');
    editBtn.className = 'px-2.5 py-1 rounded text-xs font-mono font-medium bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white border border-slate-700 flex items-center gap-1 cursor-pointer transition';
    editBtn.innerHTML = '<i data-lucide="edit-2" class="w-3.5 h-3.5"></i> Edit';
    editBtn.addEventListener('click', () => openEditModal(mem));
    actions.appendChild(editBtn);

    // 3. Supersede Button
    const isSuperseded = Boolean(mem.superseded);
    const supersedeBtn = document.createElement('button');
    supersedeBtn.className = `px-2.5 py-1 rounded text-xs font-mono font-medium flex items-center gap-1 transition ${
      isSuperseded
        ? 'bg-slate-800 text-slate-400 cursor-not-allowed opacity-60'
        : 'bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border border-rose-500/40 cursor-pointer'
    }`;
    supersedeBtn.innerHTML = '<i data-lucide="archive" class="w-3.5 h-3.5"></i> ' + (isSuperseded ? 'Superseded' : 'Supersede');
    supersedeBtn.disabled = isSuperseded;
    if (!isSuperseded) {
      supersedeBtn.addEventListener('click', () => openSupersedeModal(mem));
    }
    actions.appendChild(supersedeBtn);

    card.appendChild(actions);
    fragment.appendChild(card);
  });

  container.appendChild(fragment);
  if (window.lucide) window.lucide.createIcons(container);
}

// Action Handlers
async function handleApprove(id, kind) {
  try {
    const res = await api.approveMemory(id, kind);
    if (res.status === 'ok') {
      toast(`Memory '${id}' approved!`, 'success');
      // Update local state
      const item = allMemories.find(m => m.id === id);
      if (item) {
        item.trust_level = 'user_approved';
        applyMemoryFilters();
      } else {
        loadMemory();
      }
    } else {
      toast(`Failed to approve: ${res.message}`, 'error');
    }
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

function openEditModal(mem) {
  editingMemoryId = mem.id;
  editingMemoryKind = mem.kind || 'fact';

  const modal = document.getElementById('modal-memory-edit');
  const title = document.getElementById('memory-edit-title');
  const textarea = document.getElementById('memory-edit-text');

  if (title) title.textContent = `Edit Memory (${mem.id})`;
  if (textarea) textarea.value = mem.text || '';

  openModal('modal-memory-edit');
}

export async function submitEditMemory() {
  if (!editingMemoryId) return;
  const textarea = document.getElementById('memory-edit-text');
  const newText = textarea ? textarea.value : '';

  try {
    const res = await api.editMemory(editingMemoryId, newText, editingMemoryKind);
    if (res.status === 'ok') {
      toast('Memory updated successfully', 'success');
      closeModal('modal-memory-edit');
      const item = allMemories.find(m => m.id === editingMemoryId);
      if (item) {
        item.text = newText;
        applyMemoryFilters();
      } else {
        loadMemory();
      }
    } else {
      toast(`Update failed: ${res.message}`, 'error');
    }
  } catch (err) {
    toast(`Error updating memory: ${err.message}`, 'error');
  }
}

let supersedingMemoryId = null;

function openSupersedeModal(mem) {
  supersedingMemoryId = mem.id;

  const modal = document.getElementById('modal-memory-supersede');
  const title = document.getElementById('memory-supersede-title');
  const reasonInput = document.getElementById('memory-supersede-reason');
  const byInput = document.getElementById('memory-supersede-by');

  if (title) title.textContent = `Supersede Memory (${mem.id})`;
  if (reasonInput) reasonInput.value = '';
  if (byInput) byInput.value = '';

  openModal('modal-memory-supersede');
}

export async function submitSupersedeMemory() {
  if (!supersedingMemoryId) return;
  const reasonInput = document.getElementById('memory-supersede-reason');
  const byInput = document.getElementById('memory-supersede-by');

  const reason = reasonInput ? reasonInput.value.trim() : '';
  const supersededBy = byInput ? byInput.value.trim() : null;

  if (!reason) {
    toast('Reason is required to supersede memory', 'warning');
    return;
  }

  try {
    const res = await api.supersedeMemory(supersedingMemoryId, reason, supersededBy);
    if (res.status === 'ok') {
      toast(`Memory '${supersedingMemoryId}' marked as superseded`, 'success');
      closeModal('modal-memory-supersede');
      const item = allMemories.find(m => m.id === supersedingMemoryId);
      if (item) {
        item.superseded = true;
        applyMemoryFilters();
      } else {
        loadMemory();
      }
    } else {
      toast(`Supersede failed: ${res.message}`, 'error');
    }
  } catch (err) {
    toast(`Error superseding memory: ${err.message}`, 'error');
  }
}
