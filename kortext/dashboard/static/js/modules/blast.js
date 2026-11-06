/**
 * Kortext Dashboard - Blast-Radius & Impact Explorer
 * Multi-hop upstream/downstream impact analysis, risk scoring, and call-site tree.
 */

import { state } from './state.js';
import * as api from './api.js';
import { formatEdgesNonOverlapping } from './graph.js';
import { toast, escapeHtml, createVisTooltip, renderErrorState, renderEmptyState } from './ui.js';

function safeDomId(val) {
  return encodeURIComponent(String(val || '')).replace(/[^a-zA-Z0-9_-]/g, '_');
}

export async function initBlastTab() {
  await loadBlastAutocomplete();
  const input = document.getElementById('blast-target-input');
  if (input && !input.value.trim()) {
    input.value = 'SymbolGraphManager';
    executeBlastAnalysis();
  }
}

export async function loadBlastAutocomplete() {
  const datalist = document.getElementById('blast-suggestions');
  if (!datalist || datalist.children.length > 0) return;
  try {
    const data = await api.getSymbolsAutocomplete(150);
    if (data.status === 'ok' && data.items) {
      datalist.innerHTML = data.items.map(item => `
        <option value="${escapeHtml(item.name)}">${escapeHtml(item.name)} (${escapeHtml(item.type)})</option>
      `).join('');
    }
  } catch (err) {
    console.warn('Blast autocomplete error:', err);
  }
}

export async function executeBlastAnalysis() {
  const input = document.getElementById('blast-target-input');
  const target = input ? input.value.trim() : '';
  if (!target) return;

  const direction = document.getElementById('blast-direction') ? document.getElementById('blast-direction').value : 'upstream';
  const depth = document.getElementById('blast-depth') ? document.getElementById('blast-depth').value : '3';
  const treeContainer = document.getElementById('blast-tree-container');
  if (treeContainer) {
    treeContainer.innerHTML = '<div class="text-slate-400 py-12 text-center font-mono text-xs">Computing blast radius & dependencies...</div>';
  }

  try {
    const data = await api.getSymbolImpact(target, direction, depth, state.activeProject);
    if (data.status !== 'ok') {
      if (treeContainer) treeContainer.innerHTML = `<div class="p-3 text-rose-400 text-xs font-mono">Analysis failed: ${escapeHtml(data.message)}</div>`;
      return;
    }

    const s = data.summary || {};
    const tgtEl = document.getElementById('blast-stat-target');
    if (tgtEl) {
      tgtEl.textContent = data.target || target;
      tgtEl.title = data.target || target;
    }
    const typeEl = document.getElementById('blast-stat-type');
    if (typeEl) typeEl.textContent = s.type || 'symbol';
    const dirEl = document.getElementById('blast-stat-direct');
    if (dirEl) dirEl.textContent = (s.direct_impact_count || 0).toLocaleString();
    const transEl = document.getElementById('blast-stat-transitive');
    if (transEl) transEl.textContent = ((s.type_hierarchy_count || 0) + (s.transitive_count || 0)).toLocaleString();
    const totEl = document.getElementById('blast-stat-total');
    if (totEl) totEl.textContent = (s.total_impacted_locations || 0).toLocaleString();

    const riskEl = document.getElementById('blast-stat-risk');
    if (riskEl) {
      const risk = s.risk_level || 'LOW';
      if (risk === 'HIGH') {
        riskEl.className = 'text-xs font-mono px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/40 font-bold';
      } else if (risk === 'MEDIUM') {
        riskEl.className = 'text-xs font-mono px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40 font-bold';
      } else {
        riskEl.className = 'text-xs font-mono px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 font-bold';
      }
      riskEl.textContent = risk;
    }

    renderBlastGraph(data.nodes || [], data.edges || []);
    renderBlastTree(data.tree || []);
  } catch (err) {
    if (treeContainer) renderErrorState(treeContainer, err.message, executeBlastAnalysis);
    toast('Blast analysis failed: ' + err.message, 'error');
  }
}

export function renderBlastGraph(nodes, edges) {
  const container = document.getElementById('blast-network-graph');
  if (!container) return;

  const formattedNodes = nodes.map(n => {
    let icon = '•';
    let border = '#6366f1';
    let shadowColor = 'rgba(99, 102, 241, 0.25)';

    if (n.level === 0 || n.group === 'target') {
      icon = '🎯'; border = '#818cf8'; shadowColor = 'rgba(129, 140, 248, 0.45)';
    } else if (n.level === 1) {
      icon = '💥'; border = '#f43f5e'; shadowColor = 'rgba(244, 63, 94, 0.35)';
    } else if (n.level === 2) {
      icon = '⚡'; border = '#fb923c'; shadowColor = 'rgba(251, 146, 60, 0.35)';
    } else {
      icon = '📦'; border = '#38bdf8'; shadowColor = 'rgba(56, 189, 248, 0.3)';
    }

    const labelText = n.label || n.id;
    const label = labelText.startsWith(icon) ? labelText : `${icon}  ${labelText}`;

    return {
      ...n,
      label,
      title: createVisTooltip(n.title),
      shape: 'box',
      shapeProperties: { borderRadius: 6 },
      color: {
        background: '#0a0f1d',
        border: border,
        highlight: { background: '#1e293b', border: '#ffffff' },
        hover: { background: '#1e293b', border: border },
      },
      font: {
        color: '#ffffff',
        face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
        size: 12,
        bold: true,
      },
      margin: n.level === 0 ? 10 : 8,
      borderWidth: n.level === 0 ? 2.5 : 2,
      borderWidthSelected: 2.5,
      shadow: { enabled: true, color: shadowColor, size: 12, x: 0, y: 3 },
    };
  });

  state.blast.nodesDataSet = new vis.DataSet(formattedNodes);
  state.blast.edgesDataSet = new vis.DataSet(formatEdgesNonOverlapping(edges));

  const BLAST_GROUPS_SPEC = {
    useDefaultGroups: false,
    target: { color: { background: '#0a0f1d', border: '#818cf8', highlight: { background: '#1e293b', border: '#ffffff' } }, font: { color: '#ffffff', face: 'ui-monospace, monospace', size: 12, bold: true } },
    file: { color: { background: '#0a0f1d', border: '#38bdf8', highlight: { background: '#1e293b', border: '#ffffff' } }, font: { color: '#ffffff', face: 'ui-monospace, monospace', size: 12, bold: true } },
    symbol: { color: { background: '#0a0f1d', border: '#10b981', highlight: { background: '#1e293b', border: '#ffffff' } }, font: { color: '#ffffff', face: 'ui-monospace, monospace', size: 12, bold: true } },
  };

  const options = {
    groups: BLAST_GROUPS_SPEC,
    physics: {
      solver: 'barnesHut',
      barnesHut: { gravitationalConstant: -16000, centralGravity: 0.08, springLength: 170, springConstant: 0.035, damping: 0.16, avoidOverlap: 1.0 },
      stabilization: { enabled: true, iterations: 140 },
    },
    interaction: { hover: true, tooltipDelay: 150 },
  };

  if (state.blast.network && typeof state.blast.network.destroy === 'function') {
    state.blast.network.destroy();
  }
  state.blast.network = new vis.Network(container, { nodes: state.blast.nodesDataSet, edges: state.blast.edgesDataSet }, options);

  state.blast.network.on('stabilizationIterationsDone', () => {
    state.blast.network.setOptions({ physics: { enabled: false } });
    state.blast.isPhysicsFrozen = true;
  });
  setTimeout(() => {
    if (state.blast.network) {
      state.blast.network.setOptions({ physics: { enabled: false } });
      state.blast.isPhysicsFrozen = true;
    }
  }, 1400);

  state.blast.network.on('click', (params) => {
    if (params.nodes.length > 0) {
      const nodeId = params.nodes[0];
      const row = document.getElementById(`blast-row-${safeDomId(nodeId)}`);
      if (row) {
        row.scrollIntoView({ behavior: 'smooth', block: 'center' });
        row.classList.add('bg-slate-800');
        setTimeout(() => row.classList.remove('bg-slate-800'), 1500);
      }
    }
  });
}

export function renderBlastTree(treeItems) {
  const container = document.getElementById('blast-tree-container');
  const countEl = document.getElementById('blast-tree-count');
  if (countEl) countEl.textContent = `${treeItems.length} locations`;
  if (!container) return;

  if (!treeItems || treeItems.length === 0) {
    container.innerHTML = '<div class="text-slate-400 py-12 text-center text-xs font-mono">No callers or dependencies found.</div>';
    return;
  }

  container.innerHTML = treeItems.map(item => {
    const isHigh = item.risk === 'HIGH';
    const isMed = item.risk === 'MEDIUM';
    const badgeColor = isHigh ? 'bg-rose-500/20 text-rose-300 border-rose-500/40' : (isMed ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' : 'bg-sky-500/20 text-sky-300 border-sky-500/40');
    const targetRef = item.source || item.file_path || item.name || '';
    const safeTargetAttr = escapeHtml(String(targetRef));
    const rowId = `blast-row-${safeDomId(targetRef)}`;
    const displayPath = targetRef.split('/').slice(-2).join('/');

    return `
      <div id="${rowId}" data-node-id="${safeTargetAttr}" onclick="focusBlastNode(this.dataset.nodeId)" class="p-2.5 rounded-lg bg-slate-950/70 border border-slate-800 hover:border-slate-600 transition cursor-pointer space-y-1 font-mono text-xs">
        <div class="flex items-center justify-between">
          <span class="font-bold text-white truncate max-w-[180px]">${escapeHtml(item.name || displayPath)}</span>
          <span class="text-[9px] px-1.5 py-0.5 rounded border ${badgeColor} font-bold">${escapeHtml(item.risk)}</span>
        </div>
        <div class="flex items-center justify-between text-[10px] text-slate-400">
          <span class="text-indigo-400 font-semibold">${escapeHtml(item.relation)}</span>
          ${item.line ? `<span class="text-slate-400">Line ${escapeHtml(item.line)}</span>` : ''}
        </div>
        <div class="text-[10px] text-slate-400 truncate" title="${escapeHtml(item.file_path || item.source || '')}">
          ${escapeHtml(item.file_path || item.source || '')}
        </div>
      </div>
    `;
  }).join('');
}

export function focusBlastNode(nodeId) {
  if (!state.blast.network || !state.blast.nodesDataSet) return;
  if (state.blast.nodesDataSet.get(nodeId)) {
    state.blast.network.focus(nodeId, { scale: 1.3, animation: { duration: 500, easingFunction: 'easeInOutQuad' } });
    state.blast.network.selectNodes([nodeId]);
  }
}

export function zoomBlastGraph(factor) {
  if (!state.blast.network) return;
  state.blast.network.moveTo({ scale: state.blast.network.getScale() * factor, animation: { duration: 300 } });
}

export function fitBlastGraph() {
  if (!state.blast.network) return;
  state.blast.network.fit({ animation: { duration: 400 } });
}

export function toggleBlastFreeze() {
  if (!state.blast.network) return;
  state.blast.isPhysicsFrozen = !state.blast.isPhysicsFrozen;
  if (state.blast.isPhysicsFrozen) {
    state.blast.network.setOptions({ physics: { enabled: false } });
    state.blast.network.stopSimulation();
  } else {
    state.blast.network.setOptions({ physics: { enabled: true } });
    state.blast.network.startSimulation();
  }
  const btn = document.getElementById('btn-blast-freeze');
  if (btn) {
    btn.innerHTML = state.blast.isPhysicsFrozen ? '<i data-lucide="snowflake" class="w-3.5 h-3.5"></i> Frozen' : '<i data-lucide="flame" class="w-3.5 h-3.5 text-amber-400"></i> Simulating';
    if (window.lucide) window.lucide.createIcons();
  }
}
