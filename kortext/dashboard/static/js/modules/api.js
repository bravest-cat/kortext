/**
 * Kortext Dashboard - Centralized Async API Client
 * Wraps all dashboard backend endpoints with timeout, CSRF header, and error handling.
 */

export async function fetchJson(url, options = {}) {
  const timeoutMs = options.timeout || 15000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        'Accept': 'application/json',
        'X-Kortext-Client': 'dashboard',
        ...(options.headers || {}),
      },
    });
    clearTimeout(timer);

    if (!res.ok) {
      let errText = res.statusText;
      try {
        const errJson = await res.json();
        errText = errJson.message || errJson.detail || errText;
      } catch (_) {}
      throw new Error(`HTTP ${res.status}: ${errText}`);
    }

    return await res.json();
  } catch (err) {
    clearTimeout(timer);
    if (err.name === 'AbortError') {
      throw new Error(`Request timed out after ${timeoutMs}ms for ${url}`);
    }
    throw err;
  }
}

// 1. Health & Subsystems
export const getStatus = () => fetchJson('/api/status');
export const getTelemetry = () => fetchJson('/api/telemetry');
export const getWatcherStatus = () => fetchJson('/api/watcher/status');
export const getCacheStats = () => fetchJson('/api/cache/stats');
export const clearCache = () => fetchJson('/api/cache/clear', { method: 'POST' });
export const triggerScip = () => fetchJson('/api/scip/auto', { method: 'POST' });

// 2. Knowledge Graph
export const getGraphs = () => fetchJson('/api/graphs');
export const getGraph = (graphName, clean = true, project = null) => {
  const params = new URLSearchParams({ graph_name: graphName, clean: String(clean) });
  if (project && project !== 'all') params.append('project', project);
  return fetchJson(`/api/graph?${params.toString()}`);
};

// 3. Blast-Radius
export const getSymbolsAutocomplete = (limit = 150) =>
  fetchJson(`/api/symbols/autocomplete?limit=${limit}`);
export const getSymbolImpact = (target, direction = 'upstream', maxDepth = 3, project = null) => {
  const params = new URLSearchParams({ target, direction, max_depth: String(maxDepth) });
  if (project && project !== 'all') params.append('project', project);
  return fetchJson(`/api/symbols/impact?${params.toString()}`);
};

// 4. Tasks DAG
export const getTasks = (includeGraph = true, projectId = null) => {
  const query = new URLSearchParams({ include_graph: String(includeGraph) });
  if (projectId) query.append('project_id', projectId);
  return fetchJson(`/api/tasks?${query.toString()}`);
};
export const manageTask = (payload) =>
  fetchJson('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

// 5. Decision Journal
export const getJournal = () => fetchJson('/api/journal');

// 6. Retrieval Lab
export const runRetrieval = (payload) =>
  fetchJson('/api/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

// 7. Qdrant Collections
export const getCollections = () => fetchJson('/api/collections');
export const createCollection = (name) =>
  fetchJson('/api/collections', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });

// 8. MINJA Quarantine
export const getQuarantine = () => fetchJson('/api/quarantine');
export const clearQuarantine = () => fetchJson('/api/quarantine', { method: 'DELETE' });

// 9. Cognitive Dream Mode
export const runDream = (project = 'kortext', purgeStale = false) =>
  fetchJson('/api/dream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project, purge_stale: purgeStale }),
  });

// 10. Grounded invariants and context telemetry
export const getGroundedInvariants = () => fetchJson('/api/invariants/grounded');
export const getContextTelemetry = () => fetchJson('/api/context/telemetry');
