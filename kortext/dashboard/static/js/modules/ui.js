/**
 * Kortext Dashboard - UI Utilities, Modals, Toasts, and Tab Routing
 * Accessible UI management with WCAG AA compliance and DOM XSS sanitization.
 */

import { state } from './state.js';

export function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function sanitizeTooltipHtml(htmlStr) {
  if (!htmlStr) return '';
  return String(htmlStr)
    .replace(/<\s*(script|iframe|object|embed|svg|math|form|input|button|style|link|meta)[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, '')
    .replace(/<\s*\/?\s*(script|iframe|object|embed|svg|math|form|input|button|style|link|meta)[^>]*>/gi, '')
    .replace(/\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '')
    .replace(/javascript\s*:/gi, '');
}

export function formatDate(isoStr) {
  if (!isoStr) return '';
  try {
    const d = new Date(isoStr);
    return isNaN(d.getTime()) ? isoStr : d.toLocaleString();
  } catch (_) {
    return isoStr;
  }
}

export function createVisTooltip(titleContent) {
  if (!titleContent) return null;
  if (typeof Element !== 'undefined' && titleContent instanceof Element) {
    return titleContent;
  }
  const el = document.createElement('div');
  el.className = 'vis-custom-tooltip text-xs space-y-1';
  if (typeof titleContent === 'string' && (titleContent.includes('<') || titleContent.includes('&'))) {
    el.innerHTML = sanitizeTooltipHtml(titleContent);
  } else {
    el.textContent = String(titleContent);
  }
  return el;
}

export async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied to clipboard!', 'success');
  } catch (err) {
    console.error('Failed to copy:', err);
    toast('Failed to copy to clipboard', 'error');
  }
}

// Accessible Toast Notification System
export function toast(message, type = 'info', duration = 3500) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'fixed bottom-5 right-5 z-50 flex flex-col gap-2 pointer-events-none max-w-sm w-full';
    container.setAttribute('aria-live', 'polite');
    document.body.appendChild(container);
  }

  const toastEl = document.createElement('div');
  const typeStyles = {
    success: 'bg-emerald-950 border-emerald-500/60 text-emerald-100 shadow-emerald-950/50',
    error: 'bg-rose-950 border-rose-500/60 text-rose-100 shadow-rose-950/50',
    warning: 'bg-amber-950 border-amber-500/60 text-amber-100 shadow-amber-950/50',
    info: 'bg-indigo-950 border-indigo-500/60 text-indigo-100 shadow-indigo-950/50',
  };

  const icons = {
    success: '✅',
    error: '❌',
    warning: '⚠️',
    info: 'ℹ️',
  };

  toastEl.className = `pointer-events-auto p-3.5 rounded-xl border shadow-xl flex items-start gap-2.5 text-xs font-mono transition-all transform duration-300 opacity-0 translate-y-2 ${typeStyles[type] || typeStyles.info}`;
  toastEl.innerHTML = `
    <span class="text-sm shrink-0">${icons[type] || 'ℹ️'}</span>
    <span class="flex-1 break-words font-medium">${escapeHtml(message)}</span>
    <button class="shrink-0 text-slate-400 hover:text-white ml-1 p-0.5" onclick="this.parentElement.remove()">✕</button>
  `;

  container.appendChild(toastEl);
  requestAnimationFrame(() => {
    toastEl.classList.remove('opacity-0', 'translate-y-2');
  });

  setTimeout(() => {
    toastEl.classList.add('opacity-0', 'translate-y-2');
    setTimeout(() => toastEl.remove(), 300);
  }, duration);
}

// Modal Helpers with ESC and Backdrop Dismiss
export function openModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) {
    modal.classList.remove('hidden');
    modal.setAttribute('aria-hidden', 'false');
    const firstInput = modal.querySelector('input, textarea, button');
    if (firstInput) firstInput.focus();
    if (window.lucide) window.lucide.createIcons();
  }
}

export function closeModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) {
    modal.classList.add('hidden');
    modal.setAttribute('aria-hidden', 'true');
  }
}

// Global modal dismiss on ESC key or clicking outside content
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.fixed:not(.hidden)').forEach(modal => {
      if (modal.id && (modal.id.includes('modal') || modal.id.includes('dialog'))) {
        closeModal(modal.id);
      }
    });
  }
});

// Accessible Error and Empty States
export function renderErrorState(container, errorMsg, retryFn) {
  if (!container) return;
  container.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'p-6 text-center space-y-3 font-mono text-xs text-rose-400 bg-rose-950/20 border border-rose-800/40 rounded-xl my-4';

  const title = document.createElement('div');
  title.className = 'font-semibold text-rose-300';
  title.textContent = `Error: ${errorMsg || 'Failed to load data'}`;
  box.appendChild(title);

  if (retryFn && typeof retryFn === 'function') {
    const btn = document.createElement('button');
    btn.className = 'px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 inline-flex items-center gap-1.5 transition text-xs font-mono cursor-pointer';
    btn.setAttribute('data-lucide', 'refresh-cw');
    btn.innerHTML = '<i data-lucide="refresh-cw" class="w-3.5 h-3.5"></i> Retry';
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      retryFn();
    });
    box.appendChild(btn);
  }
  container.appendChild(box);
  if (window.lucide) window.lucide.createIcons(container);
}

export function renderEmptyState(container, message = 'nothing indexed yet, run kortext init') {
  if (!container) return;
  container.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'p-8 text-center space-y-2 font-mono text-xs text-slate-400 bg-slate-900/40 border border-dashed border-slate-800 rounded-xl my-4';
  const text = document.createElement('div');
  text.textContent = message;
  box.appendChild(text);
  container.appendChild(box);
}

// Tab Switcher Router with ARIA & URL hash preservation
export function setTab(tab, updateHash = true) {
  const validTabs = ['overview', 'graph', 'tasks', 'blast', 'journal', 'memory', 'retrieval', 'collections', 'health'];
  if (!validTabs.includes(tab)) tab = 'overview';

  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.remove('border-indigo-500', 'text-indigo-400');
    btn.classList.add('border-transparent', 'text-slate-400');
    btn.setAttribute('aria-selected', 'false');
    btn.setAttribute('tabindex', '-1');
  });
  document.querySelectorAll('.tab-content').forEach(content => content.classList.add('hidden'));

  const activeBtn = document.getElementById(`tab-btn-${tab}`);
  const activeContent = document.getElementById(`tab-content-${tab}`);
  if (activeBtn) {
    activeBtn.classList.add('border-indigo-500', 'text-indigo-400');
    activeBtn.classList.remove('border-transparent', 'text-slate-400');
    activeBtn.setAttribute('aria-selected', 'true');
    activeBtn.setAttribute('tabindex', '0');
  }
  if (activeContent) activeContent.classList.remove('hidden');

  state.activeTab = tab;

  if (updateHash && window.location.hash !== `#${tab}`) {
    history.replaceState(null, '', `#${tab}`);
  }

  // Dispatch tab-specific loaders via window hooks
  if (tab === 'overview' && window.loadOverview) window.loadOverview();
  if (tab === 'graph' && window.loadGraph) {
    if (window.populateGraphSelector) {
      window.populateGraphSelector().then(() => {
        if (!state.graph.network) window.loadGraph();
      });
    } else {
      window.loadGraph();
    }
  }
  if (tab === 'tasks' && window.loadTasks) window.loadTasks();
  if (tab === 'blast' && window.initBlastTab) window.initBlastTab();
  if (tab === 'journal' && window.loadJournal) window.loadJournal();
  if (tab === 'memory' && window.loadMemory) window.loadMemory();
  if (tab === 'health' && window.loadHealth) window.loadHealth();
  if (tab === 'collections' && window.loadCollections) window.loadCollections();
  if (tab === 'retrieval' && window.populateRetrievalCollections) window.populateRetrievalCollections();

  setTimeout(() => {
    if (window.lucide) window.lucide.createIcons();
  }, 50);
}

// Arrow-key tab navigation and hash synchronization
export function setupTabNavigation() {
  const tablist = document.getElementById('main-tablist');
  if (tablist) {
    const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
    tablist.addEventListener('keydown', (e) => {
      const currentTab = document.activeElement;
      const currentIndex = tabs.indexOf(currentTab);
      if (currentIndex === -1) return;

      let targetIndex = -1;
      if (e.key === 'ArrowRight') {
        targetIndex = (currentIndex + 1) % tabs.length;
      } else if (e.key === 'ArrowLeft') {
        targetIndex = (currentIndex - 1 + tabs.length) % tabs.length;
      } else if (e.key === 'Home') {
        targetIndex = 0;
      } else if (e.key === 'End') {
        targetIndex = tabs.length - 1;
      }

      if (targetIndex !== -1) {
        e.preventDefault();
        const targetTab = tabs[targetIndex];
        targetTab.focus();
        const tabName = targetTab.id.replace('tab-btn-', '');
        setTab(tabName);
      }
    });

    tabs.forEach(tabBtn => {
      tabBtn.addEventListener('click', () => {
        const tabName = tabBtn.id.replace('tab-btn-', '');
        setTab(tabName);
      });
    });
  }

  window.addEventListener('hashchange', () => {
    const hash = window.location.hash.slice(1);
    if (hash && hash !== state.activeTab) {
      setTab(hash, false);
    }
  });

  // Activate tab from URL hash if provided
  const initialHash = window.location.hash.slice(1);
  const validTabs = ['overview', 'graph', 'tasks', 'blast', 'journal', 'memory', 'retrieval', 'collections', 'health'];
  const initialTab = validTabs.includes(initialHash) ? initialHash : 'overview';
  setTab(initialTab, false);
}

