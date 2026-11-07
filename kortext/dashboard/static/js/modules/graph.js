/**
 * Kortext Dashboard - Knowledge Graph Explorer
 * Anti-hairball clustering, non-overlapping curved edges, ego-network isolation, and companion sidebar.
 */

import { state } from './state.js';
import * as api from './api.js';
import { toast, escapeHtml, createVisTooltip, renderErrorState, renderEmptyState } from './ui.js';
import {
  initRelationshipPalette,
  toggleRelFilter,
  setAllRelFilters,
  applyRelPreset,
  openDrawer,
  closeDrawer,
  openClusterDrawer,
} from './graph_inspector.js';

export function getEdgeStyle(label, isMention = false) {
  const lbl = (label || '').toUpperCase().trim();

  if (lbl === 'DEFINED_IN') {
    return { color: '#06b6d4', highlight: '#22d3ee', hover: '#38bdf8', width: 1.8, dashes: false, fontColor: '#38bdf8', badgeBg: 'rgba(8, 20, 36, 0.94)' };
  }
  if (lbl === 'REFERENCES' || lbl.startsWith('REFERENCES') || lbl === 'CALLS' || lbl.startsWith('CALLS')) {
    return { color: '#10b981', highlight: '#34d399', hover: '#6ee7b7', width: 2.0, dashes: false, fontColor: '#34d399', badgeBg: 'rgba(6, 28, 20, 0.94)' };
  }
  if (lbl === 'IMPORTS' || lbl.includes('IMPORTS')) {
    return { color: '#8b5cf6', highlight: '#a78bfa', hover: '#c4b5fd', width: 1.8, dashes: false, fontColor: '#a78bfa', badgeBg: 'rgba(22, 14, 46, 0.94)' };
  }
  if (lbl === 'IMPLEMENTS') {
    return { color: '#ec4899', highlight: '#f472b6', hover: '#fbcfe8', width: 2.0, dashes: [6, 4], fontColor: '#f472b6', badgeBg: 'rgba(38, 10, 24, 0.94)' };
  }
  if (lbl === 'TYPE_OF' || lbl === 'TYPE' || lbl === 'TAG') {
    return { color: '#f97316', highlight: '#fb923c', hover: '#fed7aa', width: 1.8, dashes: [3, 4], fontColor: '#fb923c', badgeBg: 'rgba(36, 16, 6, 0.94)' };
  }
  if (lbl.includes('DEPENDS') || lbl === 'BLOCKS') {
    return { color: '#f43f5e', highlight: '#fb7185', hover: '#fda4af', width: 2.0, dashes: false, fontColor: '#fb7185', badgeBg: 'rgba(36, 8, 14, 0.94)' };
  }
  if (lbl === 'SUPERSEDES') {
    return { color: '#ef4444', highlight: '#f87171', hover: '#fca5a5', width: 2.2, dashes: [6, 3], fontColor: '#f87171', badgeBg: 'rgba(40, 8, 8, 0.94)' };
  }
  if (lbl === 'RELATES_TO' || lbl === 'HAS_TASK' || lbl === 'PRODUCED_OUTCOME') {
    return { color: '#0ea5e9', highlight: '#38bdf8', hover: '#7dd3fc', width: 1.8, dashes: false, fontColor: '#38bdf8', badgeBg: 'rgba(8, 24, 40, 0.94)' };
  }
  if (lbl === 'BELONGS_TO') {
    return { color: '#a855f7', highlight: '#c084fc', hover: '#d8b4fe', width: 2.0, dashes: [4, 4], fontColor: '#c084fc', badgeBg: 'rgba(28, 10, 42, 0.94)' };
  }
  if (lbl === 'LINKS_TO') {
    return { color: '#14b8a6', highlight: '#2dd4bf', hover: '#5eead4', width: 1.8, dashes: false, fontColor: '#2dd4bf', badgeBg: 'rgba(6, 30, 28, 0.94)' };
  }
  if (isMention || lbl === 'MENTIONS') {
    return { color: '#475569', highlight: '#94a3b8', hover: '#cbd5e1', width: 1.2, dashes: [4, 4], fontColor: '#94a3b8', badgeBg: 'rgba(15, 23, 42, 0.94)' };
  }
  return { color: '#6366f1', highlight: '#818cf8', hover: '#c7d2fe', width: 1.6, dashes: false, fontColor: '#818cf8', badgeBg: 'rgba(15, 23, 42, 0.94)' };
}

export function formatEdgesNonOverlapping(edgesData) {
  const pairEdgesMap = new Map();
  edgesData.forEach(e => {
    const u = e.from < e.to ? e.from : e.to;
    const v = e.from < e.to ? e.to : e.from;
    const pairKey = `${u}|||${v}`;
    if (!pairEdgesMap.has(pairKey)) pairEdgesMap.set(pairKey, []);
    pairEdgesMap.get(pairKey).push(e);
  });

  const formatted = [];
  pairEdgesMap.forEach((edgeGroup) => {
    const total = edgeGroup.length;
    edgeGroup.forEach((e, idx) => {
      const style = getEdgeStyle(e.label, e.is_mention);
      let smooth = { enabled: true, type: 'curvedCW', roundness: 0.16 };
      if (total > 1) {
        const step = Math.floor(idx / 2);
        const isCW = (idx % 2 === 0);
        smooth = { enabled: true, type: isCW ? 'curvedCW' : 'curvedCCW', roundness: 0.18 + (step * 0.16) };
      }
      formatted.push({
        ...e,
        arrows: e.arrows || { to: { enabled: true, scaleFactor: 0.75 } },
        color: { color: style.color, highlight: style.highlight, hover: style.hover },
        originalColor: { color: style.color, highlight: style.highlight, hover: style.hover },
        width: style.width,
        originalWidth: style.width,
        smooth,
        dashes: style.dashes,
        font: { size: 9, align: 'middle', color: style.fontColor, face: 'ui-monospace, monospace', background: style.badgeBg },
        title: createVisTooltip(e.title),
      });
    });
  });
  return formatted;
}

export async function populateGraphSelector() {
  const select = document.getElementById('select-graph-type');
  if (!select) return;
  try {
    const data = await api.getGraphs();
    if (data.status === 'ok' && data.graphs && data.graphs.length > 0) {
      const currentVal = select.value || 'kortext';
      const validGraphs = data.graphs.filter(g => g !== 'default_db' && !g.endsWith('_test'));
      select.innerHTML = validGraphs.map(g => {
        let label = `Graph (${g})`;
        if (g === 'kortext') label = `🧠 Episodic & Causal Memory (${g})`;
        else if (g === 'antigravity_docs') label = '📚 Documentation & Knowledge Graph (antigravity_docs)';
        else if (g.includes('symbol')) label = `⚡ AST Symbol & Call Graph (${g})`;
        return `<option value="${g}">${label}</option>`;
      }).join('');
      if (Array.from(select.options).some(o => o.value === currentVal)) {
        select.value = currentVal;
      }
    }
  } catch (err) {
    console.warn('Could not fetch graphs list:', err);
  }
}

export async function loadGraph() {
  const statsEl = document.getElementById('graph-stats');
  if (statsEl) statsEl.textContent = 'Fetching graph data...';
  const select = document.getElementById('select-graph-type');
  const selectedDb = select ? select.value : 'kortext';
  const isSymbols = selectedDb.includes('symbol') || selectedDb.includes('doc');

  const legendEpisodic = document.getElementById('legend-episodic');
  const legendSymbols = document.getElementById('legend-symbols');
  if (legendEpisodic && legendSymbols) {
    legendEpisodic.classList.toggle('hidden', isSymbols);
    legendSymbols.classList.toggle('hidden', !isSymbols);
  }

  const clean = document.getElementById('toggle-hide-mentions') ? document.getElementById('toggle-hide-mentions').checked : true;
  const activeProj = (!isSymbols && state.activeProject && state.activeProject !== 'all') ? state.activeProject : null;
  try {
    const data = await api.getGraph(selectedDb, clean, activeProj);
    if (data.status !== 'ok') {
      if (statsEl) statsEl.textContent = 'Error: ' + data.message;
      return;
    }

    state.graph.allNodes = data.nodes || [];
    state.graph.allEdges = data.edges || [];
    initRelationshipPalette(state.graph.allEdges);

    const largeBanner = document.getElementById('large-graph-banner');
    if (state.graph.allNodes.length > 250) {
      if (largeBanner) {
        largeBanner.classList.remove('hidden');
        const msg = document.getElementById('large-graph-msg');
        if (msg) {
          msg.innerHTML = `Large graph (<strong>${state.graph.allNodes.length}</strong> nodes). Anti-hairball density filter active. <button onclick="toggleSubsystemsArchitecture()" class="ml-2 px-2 py-0.5 rounded bg-amber-600 hover:bg-amber-500 text-white font-bold transition">View Subsystems Architecture</button>`;
        }
      }
      const degSelect = document.getElementById('select-min-degree');
      if (degSelect && (degSelect.value === '1' || degSelect.value === '2')) {
        const defaultDeg = state.graph.allNodes.length > 1000 ? '4' : '3';
        degSelect.value = defaultDeg;
        state.graph.minDegreeFilter = parseInt(defaultDeg, 10);
      }
    } else if (largeBanner) {
      largeBanner.classList.add('hidden');
    }

    if (state.graph.allNodes.length === 0 && statsEl) {
      statsEl.textContent = 'nothing indexed yet, run kortext init';
    }

    applyGraphFilters();
  } catch (err) {
    if (statsEl) statsEl.textContent = 'Failed to load graph: ' + err.message;
    toast('Error loading graph: ' + err.message, 'error');
  }
}

export function applyGraphFilters() {
  const hideMentions = document.getElementById('toggle-hide-mentions') ? document.getElementById('toggle-hide-mentions').checked : false;
  let filteredEdges = state.graph.allEdges;
  if (hideMentions) filteredEdges = filteredEdges.filter(e => !e.is_mention);

  if (state.graph.activeRelFilters && state.graph.activeRelFilters.size > 0) {
    filteredEdges = filteredEdges.filter(e => state.graph.activeRelFilters.has((e.label || '').toUpperCase().trim()));
  }

  let filteredNodes = state.graph.allNodes;

  // Component Scope Filtering (Anti-Hairball)
  const compScope = state.graph.componentScope || 'all';
  if (compScope === 'subsystems') {
    filteredNodes = filteredNodes.filter(n => n.group === 'subsystem');
  } else if (compScope === 'architecture' || state.graph.isSubsystemsView) {
    filteredNodes = filteredNodes.filter(n => n.group === 'subsystem' || n.group === 'file');
  } else if (compScope === 'symbols') {
    filteredNodes = filteredNodes.filter(n => n.group === 'symbol');
  } else if (compScope === 'documents') {
    filteredNodes = filteredNodes.filter(n => n.group === 'document' || n.group === 'concept');
  }

  if (state.graph.isNeighborhoodIsolated && state.graph.isolatedFocalNodeId) {
    const hopRadius = document.getElementById('select-hop-radius') ? document.getElementById('select-hop-radius').value : '1';
    const neighborList = getNeighborhood(state.graph.isolatedFocalNodeId, hopRadius);
    const neighborSet = new Set(neighborList);
    neighborSet.add(state.graph.isolatedFocalNodeId);

    filteredNodes = filteredNodes.filter(n => neighborSet.has(n.id));
    const visNodeIds = new Set(filteredNodes.map(n => n.id));
    filteredEdges = filteredEdges.filter(e => visNodeIds.has(e.from) && visNodeIds.has(e.to));
    const hopCountEl = document.getElementById('isolation-hop-count');
    if (hopCountEl) hopCountEl.textContent = Math.max(0, filteredNodes.length - 1);
  } else {
    // If not in isolated mode or pure subsystems view, apply degree threshold
    if (!state.graph.isSubsystemsView && compScope !== 'subsystems' && state.graph.minDegreeFilter > 1) {
      filteredNodes = filteredNodes.filter(n => (n.degree || 0) >= state.graph.minDegreeFilter || n.group === 'subsystem');
    }
    const visNodeIds = new Set(filteredNodes.map(n => n.id));
    filteredEdges = filteredEdges.filter(e => visNodeIds.has(e.from) && visNodeIds.has(e.to));
  }

  const statsEl = document.getElementById('graph-stats');
  if (statsEl) {
    statsEl.textContent = `${filteredNodes.length} Visible Nodes • ${filteredEdges.length} Relationships`;
  }
  renderGraph(filteredNodes, filteredEdges);

  if (state.graph.isMacroClustered) {
    setTimeout(() => clusterAllModules(), 250);
  }
}

const VIS_GROUPS_SPEC = {
  useDefaultGroups: false,
  symbol: {
    color: { background: '#0a0f1d', border: '#10b981', highlight: { background: '#1e293b', border: '#ffffff' }, hover: { background: '#1e293b', border: '#10b981' } },
    font: { color: '#ffffff', face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', size: 12, bold: true },
  },
  file: {
    color: { background: '#0a0f1d', border: '#38bdf8', highlight: { background: '#1e293b', border: '#ffffff' }, hover: { background: '#1e293b', border: '#38bdf8' } },
    font: { color: '#ffffff', face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', size: 12, bold: true },
  },
  document: {
    color: { background: '#0a0f1d', border: '#06b6d4', highlight: { background: '#1e293b', border: '#ffffff' }, hover: { background: '#1e293b', border: '#06b6d4' } },
    font: { color: '#ffffff', face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', size: 12, bold: true },
  },
  concept: {
    color: { background: '#0a0f1d', border: '#06b6d4', highlight: { background: '#1e293b', border: '#ffffff' }, hover: { background: '#1e293b', border: '#06b6d4' } },
    font: { color: '#ffffff', face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', size: 12, bold: true },
  },
  subsystem: {
    color: { background: '#0a0f1d', border: '#a855f7', highlight: { background: '#1e293b', border: '#ffffff' }, hover: { background: '#1e293b', border: '#a855f7' } },
    font: { color: '#ffffff', face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', size: 12, bold: true },
  },
  project: {
    color: { background: '#0a0f1d', border: '#6366f1', highlight: { background: '#1e293b', border: '#ffffff' }, hover: { background: '#1e293b', border: '#6366f1' } },
    font: { color: '#ffffff', face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', size: 12, bold: true },
  },
  episodic: {
    color: { background: '#0a0f1d', border: '#f59e0b', highlight: { background: '#1e293b', border: '#ffffff' }, hover: { background: '#1e293b', border: '#f59e0b' } },
    font: { color: '#ffffff', face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', size: 12, bold: true },
  },
  decision: {
    color: { background: '#0a0f1d', border: '#f59e0b', highlight: { background: '#1e293b', border: '#ffffff' }, hover: { background: '#1e293b', border: '#f59e0b' } },
    font: { color: '#ffffff', face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', size: 12, bold: true },
  },
  task: {
    color: { background: '#0a0f1d', border: '#6366f1', highlight: { background: '#1e293b', border: '#ffffff' }, hover: { background: '#1e293b', border: '#6366f1' } },
    font: { color: '#ffffff', face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', size: 12, bold: true },
  },
};

export function renderGraph(nodesData, edgesData) {
  const container = document.getElementById('network-graph');
  if (!container) return;

  const formattedNodes = nodesData.map(n => {
    let icon = '📁';
    let border = '#38bdf8', hiBorder = '#7dd3fc';
    let shadowColor = 'rgba(56, 189, 248, 0.25)';

    if (n.group === 'episodic' || n.group === 'decision') {
      icon = '💡'; border = '#f59e0b'; hiBorder = '#fbbf24'; shadowColor = 'rgba(245, 158, 11, 0.25)';
    } else if (n.group === 'task') {
      icon = '📋'; border = '#6366f1'; hiBorder = '#818cf8'; shadowColor = 'rgba(99, 102, 241, 0.25)';
    } else if (n.group === 'project') {
      icon = '📦'; border = '#6366f1'; hiBorder = '#818cf8'; shadowColor = 'rgba(99, 102, 241, 0.25)';
    } else if (n.group === 'symbol') {
      icon = '⚡'; border = '#10b981'; hiBorder = '#34d399'; shadowColor = 'rgba(16, 185, 129, 0.25)';
    } else if (n.group === 'document' || n.group === 'concept') {
      icon = '📄'; border = '#06b6d4'; hiBorder = '#22d3ee'; shadowColor = 'rgba(6, 182, 212, 0.25)';
    } else if (n.group === 'subsystem') {
      icon = '🧩'; border = '#a855f7'; hiBorder = '#c084fc'; shadowColor = 'rgba(168, 85, 247, 0.25)';
    }

    const labelText = n.label || n.id;
    const label = labelText.startsWith(icon) ? labelText : `${icon}  ${labelText}`;

    return {
      id: n.id,
      label,
      full_name: n.full_name || n.id,
      group: n.group,
      cluster_id: n.cluster_id || 'general',
      cluster_name: n.cluster_name || 'General',
      degree: n.degree || 0,
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
      shape: 'box',
      shapeProperties: { borderRadius: 6 },
      title: createVisTooltip(n.title || `<b>${escapeHtml(n.label || n.id)}</b><br/><span style="color:#94a3b8">Type: ${escapeHtml(n.group || 'entity')}</span>`),
      margin: 10,
      borderWidth: 2,
      borderWidthSelected: 2.5,
      shadow: { enabled: true, color: shadowColor, size: 12, x: 0, y: 3 },
    };
  });

  const formattedEdges = formatEdgesNonOverlapping(edgesData);
  state.graph.nodesDataSet = new vis.DataSet(formattedNodes);
  state.graph.edgesDataSet = new vis.DataSet(formattedEdges);

  let options;
  if (state.graph.currentLayoutMode === 'hierarchical-lr') {
    options = {
      groups: VIS_GROUPS_SPEC,
      layout: {
        hierarchical: {
          enabled: true,
          direction: 'LR',
          sortMethod: 'directed',
          levelSeparation: 280,
          nodeSpacing: 180,
          treeSpacing: 220,
          blockShifting: true,
          edgeMinimization: true,
          parentCentralization: true,
        },
      },
      physics: {
        solver: 'hierarchicalRepulsion',
        hierarchicalRepulsion: { nodeDistance: 190, springLength: 200, damping: 0.25 },
      },
      interaction: { hover: true, tooltipDelay: 150 },
    };
  } else if (state.graph.currentLayoutMode === 'hierarchical-ud') {
    options = {
      groups: VIS_GROUPS_SPEC,
      layout: {
        hierarchical: {
          enabled: true,
          direction: 'UD',
          sortMethod: 'directed',
          levelSeparation: 240,
          nodeSpacing: 190,
          treeSpacing: 220,
          blockShifting: true,
          edgeMinimization: true,
          parentCentralization: true,
        },
      },
      physics: {
        solver: 'hierarchicalRepulsion',
        hierarchicalRepulsion: { nodeDistance: 190, springLength: 200, damping: 0.25 },
      },
      interaction: { hover: true, tooltipDelay: 150 },
    };
  } else {
    options = {
      groups: VIS_GROUPS_SPEC,
      physics: {
        solver: 'barnesHut',
        barnesHut: {
          gravitationalConstant: -48000,
          centralGravity: 0.012,
          springLength: 320,
          springConstant: 0.025,
          damping: 0.22,
          avoidOverlap: 1.0,
        },
        stabilization: { enabled: true, iterations: 160 },
      },
      interaction: { hover: true, tooltipDelay: 150 },
    };
  }

  if (state.graph.network && typeof state.graph.network.destroy === 'function') {
    state.graph.network.destroy();
  }
  state.graph.network = new vis.Network(container, { nodes: state.graph.nodesDataSet, edges: state.graph.edgesDataSet }, options);

  state.graph.network.on('stabilizationIterationsDone', () => {
    state.graph.network.setOptions({ physics: { enabled: false } });
    setFreezeState(true);
  });
  setTimeout(() => {
    if (state.graph.network) {
      state.graph.network.setOptions({ physics: { enabled: false } });
      setFreezeState(true);
    }
  }, 1500);

  state.graph.network.on('click', (params) => {
    if (params.nodes.length > 0) {
      const nodeId = params.nodes[0];
      if (state.graph.network.isCluster(nodeId)) {
        openClusterDrawer(nodeId);
        return;
      }
      const node = state.graph.nodesDataSet.get(nodeId);
      focusNode(nodeId);
      openDrawer(node);
    } else {
      resetGraphFocus();
      closeDrawer();
    }
  });

  state.graph.network.on('doubleClick', (params) => {
    if (params.nodes.length === 1) {
      const clickedId = params.nodes[0];
      if (state.graph.network.isCluster(clickedId)) {
        state.graph.network.openCluster(clickedId);
      }
    }
  });
}

// Macro-Clustering with Spacious Layout and 100% Reliable Revert
export function toggleMacroClustering() {
  if (!state.graph.network || !state.graph.nodesDataSet) return;
  state.graph.isMacroClustered = !state.graph.isMacroClustered;
  const btn = document.getElementById('btn-toggle-clustering');
  const label = document.getElementById('label-clustering-status');

  if (state.graph.isMacroClustered) {
    clusterAllModules();
    if (btn) btn.className = 'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-mono font-semibold bg-indigo-600 text-white shadow-lg transition';
    if (label) label.textContent = 'Macro View (ON)';
    toast('Macro-clustering enabled (Modules grouped)', 'info');
  } else {
    // 100% Deterministic Revert: re-render unclustered graph
    if (btn) btn.className = 'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-mono font-semibold bg-indigo-950/60 text-indigo-300 border border-indigo-500/40 hover:bg-indigo-900/60 transition';
    if (label) label.textContent = 'Macro View';
    applyGraphFilters();
    toast('Macro-clustering disabled (Original view restored)', 'info');
  }
}

export function clusterAllModules() {
  if (!state.graph.network || !state.graph.nodesDataSet) return;
  const allN = state.graph.nodesDataSet.get();
  const clusterGroups = new Map();
  allN.forEach(n => {
    const cid = n.cluster_id || 'general';
    const cname = n.cluster_name || cid;
    if (!clusterGroups.has(cid)) clusterGroups.set(cid, { id: cid, name: cname, count: 0 });
    clusterGroups.get(cid).count++;
  });

  // Spacious physics so macro cluster boxes have breathing room and are not cramped together
  state.graph.network.setOptions({
    physics: {
      enabled: true,
      solver: 'barnesHut',
      barnesHut: { gravitationalConstant: -55000, centralGravity: 0.01, springLength: 380, springConstant: 0.025, damping: 0.22, avoidOverlap: 1.0 },
    },
  });

  clusterGroups.forEach((info, cid) => {
    if (info.count >= 2) {
      state.graph.network.cluster({
        joinCondition: (nodeOptions) => nodeOptions.cluster_id === cid,
        processProperties: (clusterOptions, childNodes) => {
          clusterOptions.label = `📦  ${info.name}\n[${childNodes.length} components]`;
          clusterOptions.title = createVisTooltip(
            `<div style="font-weight:600;color:#c7d2fe;font-size:12px;">📦 Subsystem / Module: ${escapeHtml(info.name)}</div>` +
            `<div style="color:#cbd5e1;font-size:11px;">Contains ${childNodes.length} components.</div>` +
            `<div style="color:#94a3b8;font-size:10px;font-style:italic;">Double-click to expand</div>`
          );
          clusterOptions.shape = 'box';
          clusterOptions.margin = 12;
          clusterOptions.color = { background: '#1e1b4b', border: '#818cf8', highlight: { background: '#312e81', border: '#ffffff' }, hover: { background: '#312e81', border: '#c7d2fe' } };
          clusterOptions.font = { color: '#ffffff', face: 'ui-monospace, SFMono-Regular, Menlo, Monaco, monospace', size: 12, bold: true, multi: true };
          clusterOptions.borderWidth = 2.5;
          clusterOptions.shadow = { enabled: true, color: 'rgba(129, 140, 248, 0.45)', size: 18 };
          return clusterOptions;
        },
        clusterNodeProperties: { id: 'cluster:' + cid, borderWidth: 2.5, allowSingleNodeCluster: false },
      });
    }
  });

  setTimeout(() => {
    if (state.graph.network) {
      state.graph.network.setOptions({ physics: { enabled: false } });
      setFreezeState(true);
    }
  }, 1200);
}

// Subsystems Architecture & Anti-Hairball Mode Handlers
export function toggleSubsystemsArchitecture() {
  state.graph.isSubsystemsView = !state.graph.isSubsystemsView;
  const hudBtn = document.getElementById('btn-hud-subsystems');
  const scopeSelect = document.getElementById('select-component-scope');

  if (state.graph.isSubsystemsView) {
    if (hudBtn) hudBtn.className = 'hud-btn text-purple-300 font-bold bg-purple-950/80 border border-purple-500/50 shadow-lg';
    if (scopeSelect) scopeSelect.value = 'architecture';
    state.graph.componentScope = 'architecture';
    toast('Switched to Subsystems Architecture View (Clean Blueprint)', 'info');
  } else {
    if (hudBtn) hudBtn.className = 'hud-btn text-purple-400';
    if (scopeSelect) scopeSelect.value = 'all';
    state.graph.componentScope = 'all';
    toast('Restored Full Graph View', 'info');
  }
  applyGraphFilters();
}

export function handleComponentScopeChange(val) {
  state.graph.componentScope = val;
  state.graph.isSubsystemsView = (val === 'architecture' || val === 'subsystems');
  const hudBtn = document.getElementById('btn-hud-subsystems');
  if (hudBtn) {
    hudBtn.className = state.graph.isSubsystemsView
      ? 'hud-btn text-purple-300 font-bold bg-purple-950/80 border border-purple-500/50 shadow-lg'
      : 'hud-btn text-purple-400';
  }
  applyGraphFilters();
}

// Density, Layout, and Freeze Handlers
export function handleMinDegreeChange(val) {
  state.graph.minDegreeFilter = parseInt(val, 10) || 1;
  applyGraphFilters();
}

export function handleLayoutModeChange(val) {
  state.graph.currentLayoutMode = val;
  applyGraphFilters();
}

export function handleToggleClean() {
  loadGraph();
}

export function toggleGraphFreeze() {
  if (!state.graph.network) return;
  state.graph.isPhysicsFrozen = !state.graph.isPhysicsFrozen;
  if (state.graph.isPhysicsFrozen) {
    state.graph.network.setOptions({ physics: { enabled: false } });
    state.graph.network.stopSimulation();
    toast('Graph physics frozen', 'info');
  } else {
    state.graph.network.setOptions({ physics: { enabled: true } });
    state.graph.network.startSimulation();
    toast('Graph physics simulating', 'info');
  }
  setFreezeState(state.graph.isPhysicsFrozen);
}

export function setFreezeState(frozen) {
  state.graph.isPhysicsFrozen = frozen;
  const label = document.getElementById('label-physics-status');
  const dot = document.getElementById('dot-physics');
  const btn = document.getElementById('btn-toggle-freeze');
  const hudBtn = document.getElementById('btn-hud-freeze');

  if (frozen) {
    if (label) label.innerText = 'Frozen';
    if (dot) dot.className = 'w-2 h-2 rounded-full bg-cyan-400';
    if (btn) btn.className = 'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-mono font-semibold bg-slate-900 text-cyan-300 border border-cyan-500/40 hover:bg-slate-800 transition';
    if (hudBtn) hudBtn.innerHTML = '<i data-lucide="snowflake" class="w-3.5 h-3.5 text-cyan-400"></i> Freeze';
  } else {
    if (label) label.innerText = 'Simulating';
    if (dot) dot.className = 'w-2 h-2 rounded-full bg-amber-400 animate-ping';
    if (btn) btn.className = 'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-mono font-semibold bg-amber-950/60 text-amber-300 border border-amber-500/40 hover:bg-amber-900/60 transition';
    if (hudBtn) hudBtn.innerHTML = '<i data-lucide="flame" class="w-3.5 h-3.5 text-amber-400"></i> Simulating';
  }
  if (window.lucide) window.lucide.createIcons();
}

// Neighborhood and Search Handlers
export function getNeighborhood(startNodeId, radius) {
  if (!state.graph.network) return [];
  const r = parseInt(radius, 10) || 1;
  let currentLevel = new Set([startNodeId]);
  const visited = new Set([startNodeId]);

  for (let hop = 0; hop < r; hop++) {
    const nextLevel = new Set();
    for (const nid of currentLevel) {
      const neighbors = state.graph.network.getConnectedNodes(nid) || [];
      for (const neighbor of neighbors) {
        if (!visited.has(neighbor)) {
          visited.add(neighbor);
          nextLevel.add(neighbor);
        }
      }
    }
    currentLevel = nextLevel;
  }
  return Array.from(visited);
}

export function isolateNeighborhood(nodeId) {
  if (!nodeId) return;
  state.graph.isolatedFocalNodeId = nodeId;
  state.graph.isNeighborhoodIsolated = true;
  const banner = document.getElementById('isolation-banner');
  if (banner) {
    banner.classList.remove('hidden');
    const targetLabel = document.getElementById('isolation-target-label');
    if (targetLabel) targetLabel.textContent = nodeId.split('/').pop() || nodeId;
  }
  applyGraphFilters();
}

export function exitNeighborhoodIsolation() {
  state.graph.isNeighborhoodIsolated = false;
  state.graph.isolatedFocalNodeId = null;
  const banner = document.getElementById('isolation-banner');
  if (banner) banner.classList.add('hidden');
  applyGraphFilters();
}

export function dismissLargeGraphBanner() {
  const banner = document.getElementById('large-graph-banner');
  if (banner) banner.classList.add('hidden');
}

export function focusNode(nodeId) {
  state.graph.currentFocusedNodeId = nodeId;
  if (!state.graph.network || !state.graph.nodesDataSet) return;
  const connected = getNeighborhood(nodeId, 1);
  connected.push(nodeId);
  const connectedSet = new Set(connected);

  const nodeUpdates = state.graph.nodesDataSet.get().map(n => ({
    id: n.id,
    opacity: connectedSet.has(n.id) ? 1.0 : 0.12,
    color: n.color,
  }));
  state.graph.nodesDataSet.update(nodeUpdates);

  const edgeUpdates = state.graph.edgesDataSet.get().map(e => {
    const isConn = connectedSet.has(e.from) && connectedSet.has(e.to);
    return {
      id: e.id,
      color: isConn ? (e.originalColor || e.color) : { color: 'rgba(30, 41, 59, 0.2)' },
      width: isConn ? (e.originalWidth || 1.8) : 1,
    };
  });
  state.graph.edgesDataSet.update(edgeUpdates);
}

export function resetGraphFocus() {
  state.graph.currentFocusedNodeId = null;
  const input = document.getElementById('graph-search');
  if (input) input.value = '';
  if (state.graph.nodesDataSet && state.graph.edgesDataSet) {
    state.graph.nodesDataSet.update(state.graph.nodesDataSet.get().map(n => ({ id: n.id, opacity: 1.0, color: n.color })));
    state.graph.edgesDataSet.update(state.graph.edgesDataSet.get().map(e => ({ id: e.id, color: e.originalColor || e.color, width: e.originalWidth || 1.8 })));
  }
}

export function updateFocusRadius() {
  if (state.graph.currentFocusedNodeId) focusNode(state.graph.currentFocusedNodeId);
}

export function filterGraphBySearch() {
  const q = document.getElementById('graph-search') ? document.getElementById('graph-search').value.toLowerCase().trim() : '';
  if (!q || !state.graph.nodesDataSet) {
    resetGraphFocus();
    return;
  }
  const match = state.graph.nodesDataSet.get().find(n => (n.full_name || n.id).toLowerCase().includes(q));
  if (match) flyToNode(match.id);
}

export function flyToNode(nodeId) {
  if (!state.graph.network || !state.graph.nodesDataSet) return;
  const targetNode = state.graph.nodesDataSet.get(nodeId);
  if (targetNode) {
    state.graph.network.focus(nodeId, { scale: 1.2, animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
    focusNode(nodeId);
    openDrawer(targetNode);
  }
}

export function zoomGraph(factor) {
  if (!state.graph.network) return;
  state.graph.network.moveTo({ scale: state.graph.network.getScale() * factor, animation: { duration: 300 } });
}

export function fitGraph() {
  if (!state.graph.network) return;
  state.graph.network.fit({ animation: { duration: 400 } });
}

// Companion Sidebar & Palette Delegations
export {
  initRelationshipPalette,
  toggleRelFilter,
  setAllRelFilters,
  applyRelPreset,
  openDrawer,
  closeDrawer,
  openClusterDrawer,
} from './graph_inspector.js';

