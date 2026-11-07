/**
 * Kortext Dashboard - Decision Journal & 8-Category Taxonomy
 * Filter chips, search, interactive tag filtering, and Markdown audit viewer.
 */

import { state } from './state.js';
import * as api from './api.js';
import { toast, escapeHtml, openModal, closeModal, renderErrorState, renderEmptyState } from './ui.js';

export async function loadJournal() {
  const container = document.getElementById('journal-timeline');
  if (container) container.innerHTML = '<div class="text-slate-400 py-12 text-center text-xs font-mono">Loading journal entries...</div>';

  try {
    const data = await api.getJournal();
    state.journal.allEntries = data.entries || [];
    if (state.journal.allEntries.length === 0) {
      if (container) renderEmptyState(container, 'nothing indexed yet, run kortext init');
      updateCategoryChipsCounts();
      return;
    }
    updateCategoryChipsCounts();
    applyJournalFilters();
  } catch (err) {
    if (container) renderErrorState(container, err.message, loadJournal);
    toast('Failed to load journal: ' + err.message, 'error');
  }
}

export function updateCategoryChipsCounts() {
  const entries = state.journal.allEntries || [];
  const counts = { all: entries.length };
  entries.forEach(e => {
    const c = (e.category || 'implementation').toLowerCase();
    counts[c] = (counts[c] || 0) + 1;
  });

  const categoryLabels = {
    all: 'All',
    decision: 'Decision (ADR)',
    roadmap: 'Roadmap',
    implementation: 'Implementation',
    experiment: 'Experiment',
    benchmark: 'Benchmark',
    investigation: 'Investigation',
    research: 'Research',
    report: 'Report',
  };

  document.querySelectorAll('.journal-cat-chip').forEach(chip => {
    const cat = chip.dataset.cat;
    const baseLabel = categoryLabels[cat] || cat;
    const count = counts[cat] || 0;
    chip.textContent = `${baseLabel} (${count})`;
  });
}

export function filterJournalByCategory(category) {
  state.journal.selectedCategory = category;

  // Update active pill UI
  document.querySelectorAll('.journal-cat-chip').forEach(chip => {
    const isMatch = chip.dataset.cat === category;
    chip.className = isMatch
      ? 'journal-cat-chip px-3 py-1 rounded-full text-xs font-mono font-semibold bg-indigo-600 text-white shadow-md transition'
      : 'journal-cat-chip px-3 py-1 rounded-full text-xs font-mono bg-slate-900 text-slate-300 hover:text-white border border-slate-700 transition';
  });

  applyJournalFilters();
}

export function filterJournalByTag(tag) {
  if (state.journal.selectedTag === tag) {
    state.journal.selectedTag = null; // Toggle off if clicked again
  } else {
    state.journal.selectedTag = tag;
  }
  applyJournalFilters();
}

export function clearJournalTagFilter() {
  state.journal.selectedTag = null;
  applyJournalFilters();
}

export function filterJournal() {
  const input = document.getElementById('journal-search');
  state.journal.searchQuery = input ? input.value.toLowerCase().trim() : '';
  applyJournalFilters();
}

export function applyJournalFilters() {
  const cat = state.journal.selectedCategory;
  const tag = state.journal.selectedTag;
  const q = state.journal.searchQuery;
  const activeProj = state.activeProject;

  let entries = state.journal.allEntries;

  // Global project filter
  if (activeProj && activeProj !== 'all') {
    entries = entries.filter(e => (e.project || 'kortext').toLowerCase() === activeProj.toLowerCase());
  }

  // Strict category filter
  if (cat && cat !== 'all') {
    entries = entries.filter(e => {
      const eCat = (e.category || 'implementation').toLowerCase();
      return eCat === cat.toLowerCase();
    });
  }

  // Tag filter
  if (tag) {
    const targetTag = tag.toLowerCase();
    entries = entries.filter(e => {
      const tags = (e.tags || []).map(t => String(t).toLowerCase());
      return tags.includes(targetTag);
    });
  }

  // Text search filter
  if (q) {
    entries = entries.filter(e =>
      (e.title && e.title.toLowerCase().includes(q)) ||
      (e.body && e.body.toLowerCase().includes(q)) ||
      (e.project && e.project.toLowerCase().includes(q)) ||
      (e.tags && e.tags.some(t => String(t).toLowerCase().includes(q)))
    );
  }

  state.journal.filteredEntries = entries;
  renderJournal(entries);
}

export function renderJournal(entries) {
  const container = document.getElementById('journal-timeline');
  if (!container) return;

  let bannerHtml = '';
  if (state.journal.selectedTag) {
    bannerHtml = `
      <div class="flex items-center justify-between bg-indigo-950/60 border border-indigo-700/60 rounded-xl px-4 py-2.5 text-xs font-mono text-indigo-200">
        <div class="flex items-center gap-2">
          <span class="text-slate-400">Filtered by Tag:</span>
          <span class="px-2 py-0.5 rounded-full bg-indigo-600 text-white font-semibold">#${escapeHtml(state.journal.selectedTag)}</span>
        </div>
        <button onclick="clearJournalTagFilter()" class="text-indigo-400 hover:text-white underline transition">Clear Tag Filter</button>
      </div>
    `;
  }

  if (!entries || entries.length === 0) {
    container.innerHTML = bannerHtml + '<div class="text-slate-400 py-12 text-center text-xs font-mono">No journal entries match the selected filters.</div>';
    return;
  }

  const categoryBadges = {
    decision: 'bg-amber-500/10 text-amber-300 border-amber-500/40',
    roadmap: 'bg-sky-500/10 text-sky-300 border-sky-500/40',
    implementation: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/40',
    experiment: 'bg-purple-500/10 text-purple-300 border-purple-500/40',
    benchmark: 'bg-indigo-500/10 text-indigo-300 border-indigo-500/40',
    investigation: 'bg-rose-500/10 text-rose-300 border-rose-500/40',
    research: 'bg-cyan-500/10 text-cyan-300 border-cyan-500/40',
    report: 'bg-teal-500/10 text-teal-300 border-teal-500/40',
  };

  const cardsHtml = entries.map((e, idx) => {
    const cat = (e.category || 'implementation').toLowerCase();
    const badgeStyle = categoryBadges[cat] || categoryBadges.implementation;

    return `
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-3 hover:border-slate-700 transition">
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 pb-3">
          <div class="flex items-center gap-2.5">
            <span class="text-xs font-mono bg-indigo-500/10 text-indigo-300 px-2 py-0.5 rounded border border-indigo-500/30">${escapeHtml(e.date || '')}</span>
            <span class="text-[10px] font-mono px-2 py-0.5 rounded border uppercase font-bold ${badgeStyle}">${escapeHtml(cat)}</span>
            <h3 class="font-semibold text-white text-base">${escapeHtml(e.title || 'Untitled Outcome')}</h3>
          </div>
          <div class="flex items-center gap-2">
            ${e.project ? `<span class="text-xs font-mono bg-slate-800 text-slate-300 px-2.5 py-0.5 rounded border border-slate-700">Project: ${escapeHtml(e.project)}</span>` : ''}
            <button onclick="viewJournalMarkdown(${idx})" class="text-xs font-mono text-indigo-400 hover:text-indigo-300 hover:underline">View Raw</button>
          </div>
        </div>
        <p class="text-xs text-slate-300 font-mono whitespace-pre-wrap leading-relaxed">${escapeHtml(e.body || '')}</p>
        ${e.tags && e.tags.length > 0 ? `
          <div class="flex flex-wrap gap-1.5 pt-1">
            ${e.tags.map(t => {
              const isActive = state.journal.selectedTag && state.journal.selectedTag.toLowerCase() === String(t).toLowerCase();
              const style = isActive
                ? 'bg-indigo-600 text-white border-indigo-400 font-bold'
                : 'bg-slate-800 text-slate-400 hover:text-indigo-300 hover:bg-slate-700/80 border-slate-700/60';
              const safeTagAttr = escapeHtml(String(t));
              return `<button data-tag="${safeTagAttr}" onclick="filterJournalByTag(this.dataset.tag)" class="text-[10px] font-mono px-2 py-0.5 rounded-full border transition cursor-pointer ${style}" title="Filter by #${safeTagAttr}">#${safeTagAttr}</button>`;
            }).join('')}
          </div>
        ` : ''}
      </div>
    `;
  }).join('');

  container.innerHTML = bannerHtml + cardsHtml;
}

export function viewJournalMarkdown(index) {
  const sourceList = state.journal.filteredEntries || state.journal.allEntries || [];
  const entry = sourceList[index] || state.journal.allEntries[index];
  if (!entry) return;

  const modal = document.getElementById('modal-markdown-viewer');
  const titleEl = document.getElementById('md-viewer-title');
  const bodyEl = document.getElementById('md-viewer-body');

  if (titleEl) titleEl.textContent = entry.title || 'Journal Entry';
  if (bodyEl) {
    bodyEl.textContent = `---
title: ${entry.title}
date: ${entry.date}
category: ${entry.category || 'implementation'}
project: ${entry.project || 'kortext'}
tags: ${(entry.tags || []).join(', ')}
---

${entry.body}`;
  }

  openModal('modal-markdown-viewer');
}
