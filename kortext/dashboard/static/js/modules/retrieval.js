/**
 * Kortext Dashboard - Federated 3-Tier Retrieval Lab
 * Concurrent AST symbol seeking, dense/sparse code retrieval, and episodic memory synthesis.
 */

import { state } from './state.js';
import * as api from './api.js';
import { toast, escapeHtml } from './ui.js';

export async function populateRetrievalCollections() {
  const select = document.getElementById('retrieval-collection');
  if (!select) return;
  try {
    const data = await api.getCollections();
    if (data.status === 'ok' && data.collections) {
      const opts = ['<option value="all">🌐 All Collections (Federated)</option>'];
      data.collections.forEach(c => {
        const name = typeof c === 'string' ? c : c.name;
        opts.push(`<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`);
      });
      select.innerHTML = opts.join('');
    }
  } catch (err) {
    console.warn('Could not populate retrieval collections:', err);
  }
}

export async function executeRetrieval() {
  const queryInput = document.getElementById('retrieval-query');
  const query = queryInput ? queryInput.value.trim() : '';
  if (!query) {
    toast('Please enter a query or symbol', 'warning');
    return;
  }

  const collection = document.getElementById('retrieval-collection') ? document.getElementById('retrieval-collection').value : 'all';
  const budget = parseInt(document.getElementById('retrieval-budget') ? document.getElementById('retrieval-budget').value : '1500', 10);
  const project = state.activeProject && state.activeProject !== 'all' ? state.activeProject : undefined;

  const t0 = document.getElementById('tier0-content');
  const t1 = document.getElementById('tier1-content');
  const t2 = document.getElementById('tier2-content');

  if (t0) t0.innerHTML = '<span class="text-slate-400 font-mono text-xs">Querying Tree-sitter AST...</span>';
  if (t1) t1.innerHTML = '<span class="text-slate-400 font-mono text-xs">Computing hybrid RRF vectors...</span>';
  if (t2) t2.innerHTML = '<span class="text-slate-400 font-mono text-xs">Traversing episodic memory graph...</span>';

  try {
    const payload = { query, collection, budget, token_budget: budget };
    if (project) payload.project = project;
    const data = await api.runRetrieval(payload);
    if (data.status !== 'ok') {
      toast('Retrieval failed: ' + (data.message || 'error'), 'error');
      return;
    }

    const tokensUsed = data.total_estimated_tokens ?? data.tokens_used ?? data.token_count ?? 0;
    const badge = document.getElementById('retrieval-tokens-badge');
    const usedEl = document.getElementById('retrieval-tokens-used');
    const maxEl = document.getElementById('retrieval-tokens-max');
    if (badge) badge.classList.remove('hidden');
    if (usedEl) usedEl.textContent = tokensUsed.toLocaleString();
    if (maxEl) maxEl.textContent = budget.toLocaleString();

    // Tier 0: AST Symbols
    const symbols = data.symbols || data.symbolic_context || [];
    const t0Count = document.getElementById('tier0-count');
    if (t0Count) t0Count.textContent = `${symbols.length} symbol${symbols.length === 1 ? '' : 's'}`;
    if (t0) {
      if (symbols.length > 0) {
        t0.innerHTML = symbols.map(rawSym => {
          const s = typeof rawSym === 'object' && rawSym !== null ? rawSym : { name: String(rawSym), kind: 'symbol', signature: String(rawSym) };
          return `
            <div class="p-2.5 bg-slate-950/70 rounded-lg border border-slate-800 space-y-1 hover:border-sky-500/40 transition">
              <div class="flex items-center justify-between">
                <span class="text-xs text-sky-400 font-bold font-mono">${escapeHtml(s.name || s.symbol || '')}</span>
                <span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-sky-950/80 text-sky-300 border border-sky-800/60 uppercase">${escapeHtml(s.kind || s.type || 'symbol')}</span>
              </div>
              ${s.file_path || s.file ? `<div class="text-[10px] text-slate-400 font-mono truncate" title="${escapeHtml(s.file_path || s.file)}">${escapeHtml((s.file_path || s.file).split('/').slice(-2).join('/'))}${s.line ? `:${escapeHtml(s.line)}` : ''}</div>` : ''}
              ${s.signature ? `<pre class="text-[10px] font-mono text-slate-300 bg-slate-900/60 p-1.5 rounded overflow-x-auto">${escapeHtml(s.signature)}</pre>` : ''}
            </div>
          `;
        }).join('');
      } else {
        t0.innerHTML = '<span class="text-slate-400 text-xs font-mono">No exact AST symbols matched.</span>';
      }
    }

    // Tier 1: Code
    const chunks = data.code_chunks || [];
    const t1Count = document.getElementById('tier1-count');
    if (t1Count) t1Count.textContent = `${chunks.length} chunk${chunks.length === 1 ? '' : 's'}`;
    if (t1) {
      if (chunks.length > 0) {
        t1.innerHTML = chunks.map((c, idx) => {
          const raw = typeof c === 'string' ? c : (c.text || c.snippet || JSON.stringify(c));
          const fileMatch = raw.match(/^\[(.*?)\]\s*/);
          const sourceHeader = fileMatch ? fileMatch[1] : null;
          const snippetBody = fileMatch ? raw.slice(fileMatch[0].length) : raw;
          return `
            <div class="p-3 bg-slate-950/70 rounded-xl border border-slate-800 space-y-1.5 hover:border-emerald-500/40 transition">
              <div class="flex items-center justify-between">
                <span class="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-300 border border-emerald-800/60">#${idx + 1} RRF Rank</span>
                ${sourceHeader ? `<span class="text-[10px] font-mono text-slate-400 truncate max-w-[160px]" title="${escapeHtml(sourceHeader)}">${escapeHtml(sourceHeader.split('/').slice(-2).join('/'))}</span>` : ''}
              </div>
              <pre class="text-[11px] font-mono whitespace-pre-wrap text-slate-200 leading-relaxed max-h-52 overflow-y-auto bg-slate-900/60 p-2 rounded-lg border border-slate-800/60">${escapeHtml(snippetBody)}</pre>
            </div>
          `;
        }).join('');
      } else {
        t1.innerHTML = '<span class="text-slate-400 text-xs font-mono">No code chunks retrieved.</span>';
      }
    }

    // Tier 2: Episodic Memory
    const facts = data.episodic_history || [];
    const t2Count = document.getElementById('tier2-count');
    if (t2Count) t2Count.textContent = `${facts.length} fact${facts.length === 1 ? '' : 's'}`;
    if (t2) {
      if (facts.length > 0) {
        t2.innerHTML = facts.map(f => {
          const factText = typeof f === 'string' ? f : (f.fact || f.content || JSON.stringify(f));
          return `
            <div class="p-3 bg-slate-950/70 rounded-xl border border-slate-800 space-y-1 hover:border-purple-500/40 transition">
              <p class="text-xs text-slate-300 font-mono leading-relaxed">${escapeHtml(factText)}</p>
            </div>
          `;
        }).join('');
      } else {
        t2.innerHTML = '<span class="text-slate-400 text-xs font-mono">No episodic memories matched.</span>';
      }
    }
  } catch (err) {
    toast('Retrieval error: ' + err.message, 'error');
  }
}
