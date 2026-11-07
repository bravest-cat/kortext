/**
 * Kortext Dashboard - Task DAG State Machine & Interactive Kanban Board
 * Horizontal responsive columns, HTML5 drag-and-drop, DAG Vis.js topology, and manifest table.
 */

import { state } from './state.js';
import * as api from './api.js';
import { formatEdgesNonOverlapping } from './graph.js';
import { toast, escapeHtml, openModal, closeModal, createVisTooltip, renderErrorState, renderEmptyState } from './ui.js';

export async function loadTasks() {
  const activeProj = state.activeProject !== 'all' ? state.activeProject : null;
  try {
    const data = await api.getTasks(true, activeProj);
    if (data.status !== 'ok') {
      console.error('Failed to load tasks:', data.message);
      return;
    }

    state.tasks.taskList = data.tasks || [];
    const tasks = state.tasks.taskList;
    const nodes = data.nodes || [];
    const edges = data.edges || [];

    // Update project filter list if returned
    if (data.projects && Array.isArray(data.projects)) {
      state.availableProjects = data.projects;
      updateProjectFilterDropdown(data.projects);
    }

    const ready = tasks.filter(t => t.status === 'ready').length;
    const prog = tasks.filter(t => t.status === 'in_progress').length;
    const comp = tasks.filter(t => t.status === 'completed').length;
    const failed = tasks.filter(t => t.status === 'failed' || t.status === 'blocked').length;

    const rEl = document.getElementById('stat-ready-tasks');
    if (rEl) rEl.textContent = ready;
    const pEl = document.getElementById('stat-progress-tasks');
    if (pEl) pEl.textContent = prog;
    const cEl = document.getElementById('stat-completed-tasks');
    if (cEl) cEl.textContent = comp;
    const fEl = document.getElementById('stat-failed-tasks');
    if (fEl) fEl.textContent = failed;

    // Render active view mode
    renderKanbanBoard(tasks);
    renderTaskGraph(nodes, edges);
    renderTasksList(tasks);
  } catch (err) {
    console.error('Error loading tasks:', err);
    toast('Error loading tasks: ' + err.message, 'error');
    const container = document.getElementById('tasks-manifest-container') || document.getElementById('kanban-board-container');
    if (container) renderErrorState(container, err.message, loadTasks);
  }
}

export function setTaskViewMode(mode) {
  state.tasks.viewMode = mode;
  const btnKanban = document.getElementById('btn-view-kanban');
  const btnDag = document.getElementById('btn-view-dag');
  const btnList = document.getElementById('btn-view-list');

  const kanbanContainer = document.getElementById('kanban-board-container');
  const dagContainer = document.getElementById('task-dag-canvas-container');
  const listContainer = document.getElementById('tasks-manifest-container');

  if (btnKanban) btnKanban.className = mode === 'kanban' ? 'px-3 py-1.5 rounded-lg bg-indigo-600 text-white font-medium text-xs shadow' : 'px-3 py-1.5 rounded-lg bg-slate-800 text-slate-300 hover:text-white text-xs';
  if (btnDag) btnDag.className = mode === 'dag' ? 'px-3 py-1.5 rounded-lg bg-indigo-600 text-white font-medium text-xs shadow' : 'px-3 py-1.5 rounded-lg bg-slate-800 text-slate-300 hover:text-white text-xs';
  if (btnList) btnList.className = mode === 'list' ? 'px-3 py-1.5 rounded-lg bg-indigo-600 text-white font-medium text-xs shadow' : 'px-3 py-1.5 rounded-lg bg-slate-800 text-slate-300 hover:text-white text-xs';

  if (kanbanContainer) kanbanContainer.classList.toggle('hidden', mode !== 'kanban');
  if (dagContainer) dagContainer.classList.toggle('hidden', mode !== 'dag');
  if (listContainer) listContainer.classList.toggle('hidden', mode !== 'list');

  if (mode === 'dag' && state.tasks.network) {
    setTimeout(() => state.tasks.network.fit(), 100);
  }
}

// 1. Interactive Drag-and-Drop Kanban Board
export function renderKanbanBoard(tasks) {
  const columns = {
    ready: document.getElementById('kanban-col-ready'),
    in_progress: document.getElementById('kanban-col-in_progress'),
    pending: document.getElementById('kanban-col-pending'),
    completed: document.getElementById('kanban-col-completed'),
    failed: document.getElementById('kanban-col-failed'),
  };

  const counts = {
    ready: document.getElementById('count-ready'),
    in_progress: document.getElementById('count-in_progress'),
    pending: document.getElementById('count-pending'),
    completed: document.getElementById('count-completed'),
    failed: document.getElementById('count-failed'),
  };

  if (!columns.ready) return;

  const buckets = { ready: [], in_progress: [], pending: [], completed: [], failed: [] };
  tasks.forEach(t => {
    const st = (t.status === 'blocked' ? 'failed' : t.status) || 'pending';
    if (buckets[st]) buckets[st].push(t);
    else buckets.pending.push(t);
  });

  Object.keys(buckets).forEach(st => {
    if (counts[st]) counts[st].textContent = buckets[st].length;
    if (columns[st]) {
      if (buckets[st].length === 0) {
        columns[st].innerHTML = `<div class="p-4 text-center text-slate-400 text-xs font-mono border border-dashed border-slate-800 rounded-lg">No tasks</div>`;
      } else {
        columns[st].innerHTML = buckets[st].map(t => renderKanbanCard(t)).join('');
      }
    }
  });

  setupKanbanDropZones();
}

function renderKanbanCard(t) {
  const nextStatus = {
    ready: 'in_progress',
    in_progress: 'completed',
    pending: 'ready',
    failed: 'ready',
    completed: null,
  };

  const nextSt = nextStatus[t.status];
  const comp = t.complexity || 1;
  const compColor = comp <= 3 ? 'text-emerald-400 border-emerald-500/30 bg-emerald-500/10' : (comp <= 6 ? 'text-amber-400 border-amber-500/30 bg-amber-500/10' : 'text-rose-400 border-rose-500/30 bg-rose-500/10');

  return `
    <div id="task-card-${t.id}" draggable="true" ondragstart="handleTaskDragStart(event, '${t.id}')" class="kanban-card p-3 rounded-xl bg-slate-900 border border-slate-800 hover:border-slate-700 transition shadow space-y-2 cursor-grab active:cursor-grabbing group">
      <div class="flex items-center justify-between gap-1.5">
        <span class="font-mono text-xs font-bold text-indigo-400 group-hover:text-indigo-300 transition">${escapeHtml(t.id)}</span>
        <div class="flex items-center gap-1.5">
          <span class="text-[10px] font-mono px-1.5 py-0.5 rounded border ${compColor}" title="Complexity: ${comp}/10">C: ${comp}</span>
          ${t.assigned_to ? `<span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700 truncate max-w-[90px]">${escapeHtml(t.assigned_to)}</span>` : ''}
        </div>
      </div>
      <h4 class="font-medium text-slate-100 text-xs leading-snug break-words">${escapeHtml(t.title)}</h4>
      ${t.description ? `<p class="text-[11px] text-slate-400 line-clamp-2 leading-relaxed break-words">${escapeHtml(t.description)}</p>` : ''}
      ${t.depends_on && t.depends_on.length > 0 ? `
        <div class="text-[10px] font-mono text-amber-400/90 flex items-center gap-1 truncate" title="Prerequisites: ${t.depends_on.join(', ')}">
          <span>🔒</span> <span>Deps: ${escapeHtml(t.depends_on.join(', '))}</span>
        </div>
      ` : ''}
      ${t.last_error ? `
        <div class="text-[10px] font-mono text-rose-400 line-clamp-1 truncate" title="${escapeHtml(t.last_error)}">
          Error: ${escapeHtml(t.last_error)}
        </div>
      ` : ''}
      <div class="pt-1 flex items-center justify-between border-t border-slate-800/80">
        <span class="text-[10px] font-mono text-slate-400">${escapeHtml(t.project_id || 'kortext')}</span>
        ${nextSt ? `
          <button onclick="updateTaskStatus('${t.id}', '${nextSt}')" class="px-2 py-0.5 text-[11px] font-mono rounded bg-slate-800 hover:bg-slate-700 text-indigo-300 hover:text-white border border-slate-700 transition flex items-center gap-1" title="Advance to ${nextSt}">
            <span>→</span> <span>${nextSt.replace('_', ' ')}</span>
          </button>
        ` : '<span class="text-[10px] font-mono text-emerald-400 font-semibold">✓ Finished</span>'}
      </div>
    </div>
  `;
}

// Drag & Drop Setup
let draggedTaskId = null;

export function handleTaskDragStart(e, taskId) {
  draggedTaskId = taskId;
  e.dataTransfer.setData('text/plain', taskId);
  e.dataTransfer.effectAllowed = 'move';
}

function setupKanbanDropZones() {
  document.querySelectorAll('.kanban-column-drop').forEach(zone => {
    zone.ondragover = (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      zone.classList.add('bg-slate-800/40', 'border-indigo-500/50');
    };
    zone.ondragleave = () => {
      zone.classList.remove('bg-slate-800/40', 'border-indigo-500/50');
    };
    zone.ondrop = async (e) => {
      e.preventDefault();
      zone.classList.remove('bg-slate-800/40', 'border-indigo-500/50');
      const taskId = e.dataTransfer.getData('text/plain') || draggedTaskId;
      const targetStatus = zone.dataset.status;
      if (taskId && targetStatus) {
        await updateTaskStatus(taskId, targetStatus);
      }
    };
  });
}

// 2. Vis.js Task DAG Dependency Network
export function renderTaskGraph(nodes, edges) {
  const container = document.getElementById('task-network-graph');
  if (!container) return;

  const formattedNodes = (nodes || []).map(n => ({
    ...n,
    title: createVisTooltip(n.title || `<b>${escapeHtml(n.label || n.id)}</b><br/><span style="color:#94a3b8">Status: ${escapeHtml(n.status || '')}</span>`),
  }));
  state.tasks.nodesDataSet = new vis.DataSet(formattedNodes);
  state.tasks.edgesDataSet = new vis.DataSet(formatEdgesNonOverlapping(edges.map(e => ({
    ...e,
    label: e.label || 'DEPENDS_ON',
    title: createVisTooltip(e.title),
  }))));

  const TASK_GROUPS_SPEC = {
    useDefaultGroups: false,
    completed: { color: { background: '#0a0f1d', border: '#10b981', highlight: { background: '#1e293b', border: '#ffffff' } }, font: { color: '#ffffff', face: 'ui-monospace, monospace', size: 12, bold: true } },
    ready: { color: { background: '#0a0f1d', border: '#38bdf8', highlight: { background: '#1e293b', border: '#ffffff' } }, font: { color: '#ffffff', face: 'ui-monospace, monospace', size: 12, bold: true } },
    in_progress: { color: { background: '#0a0f1d', border: '#f59e0b', highlight: { background: '#1e293b', border: '#ffffff' } }, font: { color: '#ffffff', face: 'ui-monospace, monospace', size: 12, bold: true } },
    pending: { color: { background: '#0a0f1d', border: '#64748b', highlight: { background: '#1e293b', border: '#ffffff' } }, font: { color: '#ffffff', face: 'ui-monospace, monospace', size: 12, bold: true } },
    failed: { color: { background: '#0a0f1d', border: '#f87171', highlight: { background: '#1e293b', border: '#ffffff' } }, font: { color: '#ffffff', face: 'ui-monospace, monospace', size: 12, bold: true } },
    blocked: { color: { background: '#0a0f1d', border: '#e11d48', highlight: { background: '#1e293b', border: '#ffffff' } }, font: { color: '#ffffff', face: 'ui-monospace, monospace', size: 12, bold: true } },
  };

  const options = {
    groups: TASK_GROUPS_SPEC,
    physics: {
      solver: 'barnesHut',
      barnesHut: { gravitationalConstant: -14000, centralGravity: 0.1, springLength: 160, springConstant: 0.04, damping: 0.15, avoidOverlap: 1.0 },
      stabilization: { enabled: true, iterations: 140 },
    },
    interaction: { hover: true, tooltipDelay: 150 },
  };

  if (state.tasks.network && typeof state.tasks.network.destroy === 'function') {
    state.tasks.network.destroy();
  }
  state.tasks.network = new vis.Network(container, { nodes: state.tasks.nodesDataSet, edges: state.tasks.edgesDataSet }, options);

  state.tasks.network.on('stabilizationIterationsDone', () => {
    state.tasks.network.setOptions({ physics: { enabled: false } });
    state.tasks.isPhysicsFrozen = true;
    const st = document.getElementById('task-graph-status');
    if (st) st.innerText = 'Physics: Frozen';
  });

  setTimeout(() => {
    if (state.tasks.network) {
      state.tasks.network.setOptions({ physics: { enabled: false } });
      state.tasks.isPhysicsFrozen = true;
      const st = document.getElementById('task-graph-status');
      if (st) st.innerText = 'Physics: Frozen';
    }
  }, 1400);

  state.tasks.network.on('click', (params) => {
    if (params.nodes.length > 0) {
      const nodeId = params.nodes[0];
      const connected = state.tasks.network.getConnectedNodes(nodeId);
      connected.push(nodeId);
      const connectedSet = new Set(connected);

      state.tasks.nodesDataSet.update(state.tasks.nodesDataSet.get().map(n => ({
        id: n.id,
        opacity: connectedSet.has(n.id) ? 1.0 : 0.15,
        color: n.color,
      })));
    } else {
      state.tasks.nodesDataSet.update(state.tasks.nodesDataSet.get().map(n => ({ id: n.id, opacity: 1.0, color: n.color })));
    }
  });
}

export function zoomTaskGraph(factor) {
  if (!state.tasks.network) return;
  state.tasks.network.moveTo({ scale: state.tasks.network.getScale() * factor, animation: { duration: 300 } });
}

export function fitTaskGraph() {
  if (!state.tasks.network) return;
  state.tasks.network.fit({ animation: { duration: 400 } });
}

export function toggleTaskFreeze() {
  if (!state.tasks.network) return;
  state.tasks.isPhysicsFrozen = !state.tasks.isPhysicsFrozen;
  if (state.tasks.isPhysicsFrozen) {
    state.tasks.network.setOptions({ physics: { enabled: false } });
    state.tasks.network.stopSimulation();
  } else {
    state.tasks.network.setOptions({ physics: { enabled: true } });
    state.tasks.network.startSimulation();
  }
  const st = document.getElementById('task-graph-status');
  const btn = document.getElementById('btn-task-freeze');
  if (st) st.innerText = state.tasks.isPhysicsFrozen ? 'Physics: Frozen' : 'Physics: Simulating';
  if (btn) {
    btn.innerHTML = state.tasks.isPhysicsFrozen ? '<i data-lucide="snowflake" class="w-3.5 h-3.5"></i> Frozen' : '<i data-lucide="flame" class="w-3.5 h-3.5 text-amber-400"></i> Simulating';
    if (window.lucide) window.lucide.createIcons();
  }
}

// 3. Manifest Table View
export function renderTasksList(tasks) {
  const container = document.getElementById('tasks-container');
  if (!container) return;

  if (!tasks || tasks.length === 0) {
    container.innerHTML = '<div class="text-slate-400 py-12 text-center text-xs font-mono">No active tasks in Task DAG. Click "+ Create Task" above.</div>';
    return;
  }

  const statusBadge = {
    ready: 'bg-sky-500/10 text-sky-400 border-sky-500/30',
    in_progress: 'bg-amber-500/10 text-amber-300 border-amber-500/40 font-semibold',
    completed: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
    pending: 'bg-slate-700/20 text-slate-400 border-slate-700/40',
    failed: 'bg-rose-500/10 text-rose-400 border-rose-500/30',
    blocked: 'bg-red-900/20 text-red-400 border-red-800/40',
  };

  container.innerHTML = tasks.map(t => {
    const badge = statusBadge[t.status] || statusBadge.pending;
    return `
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-3.5 flex flex-col md:flex-row md:items-center justify-between gap-3 hover:border-slate-700 transition">
        <div class="space-y-1">
          <div class="flex items-center gap-2">
            <span class="font-mono text-xs font-semibold text-slate-400">${escapeHtml(t.id)}</span>
            <span class="text-xs font-mono px-2 py-0.5 rounded border ${badge} uppercase font-semibold">${escapeHtml(t.status)}</span>
            <span class="text-xs font-mono text-slate-400">Complexity: ${t.complexity}/10</span>
            <span class="text-xs font-mono text-slate-400">Assigned: ${escapeHtml(t.assigned_to)}</span>
            <span class="text-xs font-mono text-slate-400">Project: ${escapeHtml(t.project_id || 'kortext')}</span>
          </div>
          <h4 class="font-medium text-white text-sm break-words">${escapeHtml(t.title)}</h4>
          ${t.description ? `<p class="text-xs text-slate-400 break-words">${escapeHtml(t.description)}</p>` : ''}
          ${t.depends_on && t.depends_on.length > 0 ? `<div class="text-[11px] font-mono text-indigo-400">Depends on: ${escapeHtml(t.depends_on.join(', '))}</div>` : ''}
          ${t.last_error ? `<div class="text-[11px] font-mono text-rose-400">Last Error: ${escapeHtml(t.last_error)}</div>` : ''}
        </div>
        <div class="flex items-center gap-2 self-end md:self-center shrink-0">
          ${t.status !== 'in_progress' && t.status !== 'completed' ? `
            <button onclick="updateTaskStatus('${t.id}', 'in_progress')" class="px-2.5 py-1 text-xs rounded bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/40 font-mono transition">Start</button>
          ` : ''}
          ${t.status !== 'completed' ? `
            <button onclick="updateTaskStatus('${t.id}', 'completed')" class="px-2.5 py-1 text-xs rounded bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-300 border border-emerald-500/40 font-mono transition">Done</button>
          ` : ''}
          ${t.status !== 'failed' && t.status !== 'completed' ? `
            <button onclick="updateTaskStatus('${t.id}', 'failed', 'Marked failed via UI')" class="px-2.5 py-1 text-xs rounded bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border border-rose-500/40 font-mono transition">Fail</button>
          ` : ''}
        </div>
      </div>
    `;
  }).join('');
}

export async function updateTaskStatus(taskId, status, error = null) {
  try {
    const data = await api.manageTask({ action: 'update_status', task_id: taskId, status, error });
    if (data.status === 'ok') {
      toast(`Task ${taskId} moved to ${status}`, 'success');
      loadTasks();
    } else {
      toast(`Failed to update task: ${data.message || 'unknown error'}`, 'error');
    }
  } catch (err) {
    console.error('Task update error:', err);
    toast('Update failed: ' + err.message, 'error');
  }
}

// 4. Modal Helpers & Form Submission
export function openCreateTaskModal() {
  openModal('modal-create-task');
}

export function closeCreateTaskModal() {
  closeModal('modal-create-task');
}

export async function submitCreateTask() {
  const title = document.getElementById('task-input-title') ? document.getElementById('task-input-title').value.trim() : '';
  const desc = document.getElementById('task-input-desc') ? document.getElementById('task-input-desc').value.trim() : '';
  const comp = parseInt(document.getElementById('task-input-comp') ? document.getElementById('task-input-comp').value : '1', 10) || 1;
  const assign = document.getElementById('task-input-assign') ? document.getElementById('task-input-assign').value.trim() : 'primary';
  const proj = document.getElementById('task-input-project') ? document.getElementById('task-input-project').value.trim() : 'kortext';
  const rawDeps = document.getElementById('task-input-deps') ? document.getElementById('task-input-deps').value.trim() : '';
  const deps = rawDeps ? rawDeps.split(',').map(s => s.trim()).filter(Boolean) : null;

  if (!title) {
    toast('Please enter a task title', 'warning');
    return;
  }

  try {
    const data = await api.manageTask({
      action: 'create',
      title,
      description: desc,
      complexity: comp,
      assigned_to: assign,
      project_id: proj,
      depends_on: deps,
    });
    if (data.status === 'ok') {
      closeCreateTaskModal();
      toast(`Created task ${data.task_id}`, 'success');
      if (document.getElementById('task-input-title')) document.getElementById('task-input-title').value = '';
      if (document.getElementById('task-input-desc')) document.getElementById('task-input-desc').value = '';
      if (document.getElementById('task-input-deps')) document.getElementById('task-input-deps').value = '';
      loadTasks();
    } else {
      toast(`Task creation failed: ${data.message || 'error'}`, 'error');
    }
  } catch (err) {
    toast('Create task error: ' + err.message, 'error');
  }
}

function updateProjectFilterDropdown(projects) {
  const select = document.getElementById('select-active-project');
  if (!select) return;
  const currentVal = select.value || state.activeProject || 'all';
  select.innerHTML = projects.map(p => `
    <option value="${escapeHtml(p)}">${p === 'all' ? 'All Projects' : `Project: ${escapeHtml(p)}`}</option>
  `).join('');
  if (projects.includes(currentVal)) {
    select.value = currentVal;
  }
}

export function setActiveProject(proj) {
  state.activeProject = proj;
  import('./state.js').then(m => {
    if (m.emit) m.emit('project:changed', proj);
  }).catch(() => {});
  loadTasks();
  if (window.loadGraph && state.activeTab === 'graph') window.loadGraph();
  if (window.loadJournal && state.activeTab === 'journal') window.loadJournal();
  if (window.executeBlastAnalysis && state.activeTab === 'blast') window.executeBlastAnalysis();
  if (window.executeRetrieval && state.activeTab === 'retrieval') {
    const q = document.getElementById('retrieval-query');
    if (q && q.value.trim()) window.executeRetrieval();
  }
  if (window.loadTelemetry && state.activeTab === 'health') window.loadTelemetry();
}
