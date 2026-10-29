/**
 * Kortext Dashboard - Application Entry Point
 * Bootstraps modules, binds backward-compatibility shims onto window, and initializes views.
 */

import { state } from './modules/state.js';
import * as ui from './modules/ui.js';
import * as graph from './modules/graph.js';
import * as blast from './modules/blast.js';
import * as tasks from './modules/tasks.js';
import * as journal from './modules/journal.js';
import * as retrieval from './modules/retrieval.js';
import * as health from './modules/health.js';
import * as dream from './modules/dream.js';

// Expose all functions onto window for 100% backward compatibility with HTML onclick/onchange handlers
Object.assign(window, {
  // State
  dashboardState: state,
  state: state,

  // UI & Routing
  setTab: ui.setTab,
  toast: ui.toast,
  openModal: ui.openModal,
  closeModal: ui.closeModal,
  copyToClipboard: ui.copyToClipboard,

  // Knowledge Graph
  populateGraphSelector: graph.populateGraphSelector,
  loadGraph: graph.loadGraph,
  applyGraphFilters: graph.applyGraphFilters,
  toggleMacroClustering: graph.toggleMacroClustering,
  handleMinDegreeChange: graph.handleMinDegreeChange,
  handleLayoutModeChange: graph.handleLayoutModeChange,
  handleToggleClean: graph.handleToggleClean,
  toggleGraphFreeze: graph.toggleGraphFreeze,
  resetGraphFocus: graph.resetGraphFocus,
  filterGraphBySearch: graph.filterGraphBySearch,
  updateFocusRadius: graph.updateFocusRadius,
  flyToNode: graph.flyToNode,
  zoomGraph: graph.zoomGraph,
  fitGraph: graph.fitGraph,
  toggleRelFilter: graph.toggleRelFilter,
  setAllRelFilters: graph.setAllRelFilters,
  applyRelPreset: graph.applyRelPreset,
  isolateNeighborhood: graph.isolateNeighborhood,
  exitNeighborhoodIsolation: graph.exitNeighborhoodIsolation,
  dismissLargeGraphBanner: graph.dismissLargeGraphBanner,
  toggleSubsystemsArchitecture: graph.toggleSubsystemsArchitecture,
  handleComponentScopeChange: graph.handleComponentScopeChange,

  // Blast-Radius
  initBlastTab: blast.initBlastTab,
  loadBlastAutocomplete: blast.loadBlastAutocomplete,
  executeBlastAnalysis: blast.executeBlastAnalysis,
  focusBlastNode: blast.focusBlastNode,
  zoomBlastGraph: blast.zoomBlastGraph,
  fitBlastGraph: blast.fitBlastGraph,
  toggleBlastFreeze: blast.toggleBlastFreeze,

  // Tasks DAG & Kanban
  loadTasks: tasks.loadTasks,
  setTaskViewMode: tasks.setTaskViewMode,
  handleTaskDragStart: tasks.handleTaskDragStart,
  zoomTaskGraph: tasks.zoomTaskGraph,
  fitTaskGraph: tasks.fitTaskGraph,
  toggleTaskFreeze: tasks.toggleTaskFreeze,
  updateTaskStatus: tasks.updateTaskStatus,
  openCreateTaskModal: tasks.openCreateTaskModal,
  closeCreateTaskModal: tasks.closeCreateTaskModal,
  submitCreateTask: tasks.submitCreateTask,
  setActiveProject: tasks.setActiveProject,

  // Decision Journal
  loadJournal: journal.loadJournal,
  filterJournal: journal.filterJournal,
  filterJournalByCategory: journal.filterJournalByCategory,
  filterJournalByTag: journal.filterJournalByTag,
  clearJournalTagFilter: journal.clearJournalTagFilter,
  viewJournalMarkdown: journal.viewJournalMarkdown,

  // Retrieval Lab
  populateRetrievalCollections: retrieval.populateRetrievalCollections,
  executeRetrieval: retrieval.executeRetrieval,

  // Health, Telemetry, Collections & Quarantine
  loadHealth: health.loadHealth,
  clearChunkCache: health.clearChunkCache,
  triggerScipIngest: health.triggerScipIngest,
  loadTelemetry: health.loadTelemetry,
  loadCollections: health.loadCollections,
  createNewCollection: health.createNewCollection,
  loadQuarantine: health.loadQuarantine,
  clearQuarantine: health.clearQuarantine,
  loadContextTelemetry: health.loadContextTelemetry,
  loadGroundedInvariants: health.loadGroundedInvariants,
  refreshAll: health.refreshAll,

  // Dream Mode
  openDreamModal: dream.openDreamModal,
  closeDreamModal: dream.closeDreamModal,
  runDreamMode: dream.runDreamMode,
});

// Application Initialization
function init() {
  if (window.lucide) window.lucide.createIcons();

  // Load health and telemetry on startup
  health.loadHealth();
  health.loadTelemetry();

  // Initialize Knowledge Graph view
  graph.populateGraphSelector().then(() => {
    graph.loadGraph();
  });

  // Populate retrieval collections
  retrieval.populateRetrievalCollections();

  // 15-second heartbeat for background daemons
  setInterval(() => {
    health.loadHealth();
  }, 15000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
