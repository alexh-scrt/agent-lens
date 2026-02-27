/**
 * AgentLens Dashboard — Frontend JavaScript
 *
 * Connects to the SSE stream, renders events in a timeline, and handles
 * filter/search UI interactions.
 *
 * Architecture:
 * - EventStream: manages the SSE connection and reconnect logic.
 * - EventStore: in-memory list of events for filtering/searching.
 * - UI: renders events, handles user interaction.
 */

'use strict';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const API_BASE = '';
const SSE_URL = `${API_BASE}/api/events/stream`;
const EVENTS_URL = `${API_BASE}/api/events`;
const STATS_URL = `${API_BASE}/api/stats`;

const MAX_DISPLAY_EVENTS = 1000;
const STATS_REFRESH_INTERVAL_MS = 10_000;
const RECONNECT_DELAY_MS = 3_000;

/** Color classes and icons for each event type. */
const EVENT_TYPE_CONFIG = {
  file_create:   { badge: 'badge-file_create',   icon: '✚', label: 'CREATE' },
  file_modify:   { badge: 'badge-file_modify',   icon: '✎', label: 'MODIFY' },
  file_delete:   { badge: 'badge-file_delete',   icon: '✖', label: 'DELETE' },
  file_read:     { badge: 'badge-file_read',     icon: '👁', label: 'READ'   },
  shell_command: { badge: 'badge-shell_command', icon: '⚡', label: 'SHELL'  },
  network_call:  { badge: 'badge-network_call',  icon: '🌐', label: 'NETWORK'},
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let allEvents = [];        // All events received this session
let filteredEvents = [];   // Events after applying active filters
let selectedEventId = null;
let liveEnabled = true;
let autoScrollEnabled = true;
let eventSource = null;
let reconnectTimer = null;
let statsRefreshTimer = null;

// Active filter state
let activeFilters = {
  eventType: '',
  processName: '',
  pathContains: '',
  search: '',
};

// ---------------------------------------------------------------------------
// DOM References (populated after DOMContentLoaded)
// ---------------------------------------------------------------------------

let elEventList;
let elEmptyState;
let elStatusDot;
let elStatusText;
let elSubscriberCount;
let elDisplayCount;
let elDetailPanel;
let elDetailContent;

// ---------------------------------------------------------------------------
// Utility functions
// ---------------------------------------------------------------------------

/**
 * Format a UTC ISO timestamp into a human-readable local time string.
 * @param {string} iso
 * @returns {string}
 */
function formatTimestamp(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      fractionalSecondDigits: 3,
    });
  } catch {
    return iso;
  }
}

/**
 * Format a full date + time.
 * @param {string} iso
 * @returns {string}
 */
function formatFullTimestamp(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

/**
 * Escape HTML special characters.
 * @param {string} str
 * @returns {string}
 */
function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Truncate a string to a max length with ellipsis.
 * @param {string} str
 * @param {number} maxLen
 * @returns {string}
 */
function truncate(str, maxLen = 80) {
  if (!str) return '';
  return str.length > maxLen ? str.slice(0, maxLen) + '…' : str;
}

/**
 * Get the primary display text for an event (path, command, or host).
 * @param {Object} event
 * @returns {string}
 */
function getEventPrimary(event) {
  if (event.path) return event.path;
  if (event.command_line) return event.command_line;
  if (event.remote_host) {
    return `${event.remote_host}:${event.remote_port || '?'}`;
  }
  return '—';
}

// ---------------------------------------------------------------------------
// Event filtering
// ---------------------------------------------------------------------------

/**
 * Apply active filters to the full events list and rebuild filteredEvents.
 */
function applyFilters() {
  const { eventType, processName, pathContains, search } = activeFilters;
  const searchLower = search.toLowerCase();

  filteredEvents = allEvents.filter(evt => {
    if (eventType && evt.event_type !== eventType) return false;
    if (processName && !(evt.process_name || '').toLowerCase().includes(processName.toLowerCase())) return false;
    if (pathContains && !(evt.path || '').toLowerCase().includes(pathContains.toLowerCase())) return false;
    if (search) {
      const haystack = [
        evt.path || '',
        evt.command_line || '',
        evt.remote_host || '',
        evt.process_name || '',
        evt.event_type || '',
        JSON.stringify(evt.metadata || {}),
      ].join(' ').toLowerCase();
      if (!haystack.includes(searchLower)) return false;
    }
    return true;
  });

  renderEventList();
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

/**
 * Build the HTML for a single event row.
 * @param {Object} event
 * @returns {HTMLElement}
 */
function buildEventRow(event) {
  const config = EVENT_TYPE_CONFIG[event.event_type] || {
    badge: 'badge-default',
    icon: '?',
    label: event.event_type,
  };

  const row = document.createElement('div');
  row.className = 'event-row';
  row.dataset.eventId = event.id;
  if (event.id === selectedEventId) {
    row.classList.add('selected');
  }

  const primaryText = truncate(getEventPrimary(event), 120);
  const processInfo = event.process_name
    ? `<span class="event-process">${escapeHtml(event.process_name)}${event.process_pid ? ':' + event.process_pid : ''}</span>`
    : '';

  row.innerHTML = `
    <span class="event-time" title="${escapeHtml(formatFullTimestamp(event.timestamp))}">
      ${escapeHtml(formatTimestamp(event.timestamp))}
    </span>
    <span class="badge ${config.badge}" title="${escapeHtml(event.event_type)}">
      ${config.icon} ${config.label}
    </span>
    <span class="event-primary" title="${escapeHtml(getEventPrimary(event))}">
      ${escapeHtml(primaryText)}
    </span>
    ${processInfo}
  `;

  row.addEventListener('click', () => selectEvent(event));
  return row;
}

/**
 * Render the full filtered event list into the DOM.
 */
function renderEventList() {
  if (!elEventList) return;

  const events = filteredEvents.slice(-MAX_DISPLAY_EVENTS);

  // Show/hide empty state
  if (elEmptyState) {
    elEmptyState.style.display = events.length === 0 ? 'flex' : 'none';
  }

  // Remove existing rows (but keep the empty-state element)
  const existingRows = elEventList.querySelectorAll('.event-row');
  existingRows.forEach(el => el.remove());

  // Append rows
  const fragment = document.createDocumentFragment();
  events.forEach(evt => {
    fragment.appendChild(buildEventRow(evt));
  });
  elEventList.appendChild(fragment);

  // Update display count
  if (elDisplayCount) {
    elDisplayCount.textContent = events.length;
  }

  // Auto-scroll to bottom
  if (autoScrollEnabled) {
    elEventList.scrollTop = elEventList.scrollHeight;
  }
}

/**
 * Append a single new event row to the end of the list (efficient for live mode).
 * @param {Object} event
 */
function appendEventRow(event) {
  if (!elEventList) return;

  // Hide empty state
  if (elEmptyState) {
    elEmptyState.style.display = 'none';
  }

  // Evict oldest rows if over limit
  const existingRows = elEventList.querySelectorAll('.event-row');
  if (existingRows.length >= MAX_DISPLAY_EVENTS) {
    existingRows[0].remove();
  }

  const row = buildEventRow(event);
  elEventList.appendChild(row);

  if (elDisplayCount) {
    const currentCount = parseInt(elDisplayCount.textContent, 10) || 0;
    elDisplayCount.textContent = Math.min(currentCount + 1, MAX_DISPLAY_EVENTS);
  }

  if (autoScrollEnabled) {
    elEventList.scrollTop = elEventList.scrollHeight;
  }
}

/**
 * Render the detail panel for a selected event.
 * @param {Object} event
 */
function renderDetailPanel(event) {
  if (!elDetailContent) return;

  const config = EVENT_TYPE_CONFIG[event.event_type] || { badge: 'badge-default', icon: '?', label: event.event_type };

  const metaRows = Object.entries(event.metadata || {}).map(([k, v]) =>
    `<tr><td class="detail-key">${escapeHtml(k)}</td><td>${escapeHtml(JSON.stringify(v))}</td></tr>`
  ).join('');

  elDetailContent.innerHTML = `
    <div class="detail-type">
      <span class="badge ${config.badge}">${config.icon} ${event.event_type}</span>
    </div>
    <table class="detail-table">
      <tr>
        <td class="detail-key">ID</td>
        <td><code class="detail-id">${escapeHtml(event.id)}</code></td>
      </tr>
      <tr>
        <td class="detail-key">Timestamp</td>
        <td>${escapeHtml(formatFullTimestamp(event.timestamp))}</td>
      </tr>
      ${event.path ? `<tr><td class="detail-key">Path</td><td><code>${escapeHtml(event.path)}</code></td></tr>` : ''}
      ${event.process_name ? `<tr><td class="detail-key">Process</td><td>${escapeHtml(event.process_name)}</td></tr>` : ''}
      ${event.process_pid ? `<tr><td class="detail-key">PID</td><td>${escapeHtml(String(event.process_pid))}</td></tr>` : ''}
      ${event.command_line ? `<tr><td class="detail-key">Command</td><td><code>${escapeHtml(event.command_line)}</code></td></tr>` : ''}
      ${event.remote_host ? `<tr><td class="detail-key">Remote Host</td><td>${escapeHtml(event.remote_host)}</td></tr>` : ''}
      ${event.remote_port ? `<tr><td class="detail-key">Remote Port</td><td>${escapeHtml(String(event.remote_port))}</td></tr>` : ''}
      ${metaRows}
    </table>
  `;

  if (elDetailPanel) {
    elDetailPanel.classList.add('open');
  }
}

/**
 * Select an event and show its details.
 * @param {Object} event
 */
function selectEvent(event) {
  selectedEventId = event.id;

  // Update row highlights
  document.querySelectorAll('.event-row').forEach(row => {
    row.classList.toggle('selected', row.dataset.eventId === event.id);
  });

  renderDetailPanel(event);
}

/**
 * Close the detail panel.
 */
function closeDetailPanel() {
  selectedEventId = null;
  if (elDetailPanel) {
    elDetailPanel.classList.remove('open');
  }
  document.querySelectorAll('.event-row.selected').forEach(row => {
    row.classList.remove('selected');
  });
}

// ---------------------------------------------------------------------------
// Status indicators
// ---------------------------------------------------------------------------

/**
 * Set the connection status indicator.
 * @param {'connected'|'connecting'|'disconnected'} state
 */
function setConnectionStatus(state) {
  if (!elStatusDot || !elStatusText) return;
  elStatusDot.className = `status-dot status-${state}`;
  const labels = {
    connected: 'Live',
    connecting: 'Connecting…',
    disconnected: 'Disconnected',
  };
  elStatusText.textContent = labels[state] || state;
}

// ---------------------------------------------------------------------------
// Stats loading
// ---------------------------------------------------------------------------

/**
 * Fetch and display aggregate statistics from the API.
 */
async function loadStats() {
  try {
    const resp = await fetch(STATS_URL);
    if (!resp.ok) return;
    const data = await resp.json();

    const totalEl = document.getElementById('stat-total');
    if (totalEl) totalEl.textContent = data.total_events ?? '--';

    for (const [type, count] of Object.entries(data.by_type || {})) {
      const el = document.getElementById(`stat-${type}`);
      if (el) el.textContent = count;
    }

    if (elSubscriberCount) {
      elSubscriberCount.textContent = `👤 ${data.subscriber_count ?? '--'}`;
    }
  } catch (err) {
    // Stats are non-critical — swallow errors
    console.debug('Stats fetch error:', err);
  }
}

// ---------------------------------------------------------------------------
// History loading
// ---------------------------------------------------------------------------

/**
 * Load historical events from the REST API and prepend them to the display.
 */
async function loadHistory() {
  try {
    const params = new URLSearchParams({ limit: '200', offset: '0' });
    if (activeFilters.eventType) params.set('event_type', activeFilters.eventType);
    if (activeFilters.processName) params.set('process_name', activeFilters.processName);
    if (activeFilters.pathContains) params.set('path_contains', activeFilters.pathContains);

    const resp = await fetch(`${EVENTS_URL}?${params.toString()}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    const events = data.events || [];
    if (events.length === 0) return;

    // Merge with existing events (avoid duplicates by id)
    const existingIds = new Set(allEvents.map(e => e.id));
    const newEvents = events.filter(e => !existingIds.has(e.id));

    // Prepend historical events (they are older)
    allEvents = [...newEvents, ...allEvents];
    applyFilters();
  } catch (err) {
    console.error('Failed to load history:', err);
  }
}

// ---------------------------------------------------------------------------
// SSE connection
// ---------------------------------------------------------------------------

/**
 * Connect to the SSE event stream.
 */
function connectSSE() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }

  setConnectionStatus('connecting');

  eventSource = new EventSource(SSE_URL);

  eventSource.onopen = () => {
    setConnectionStatus('connected');
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    loadStats();
  };

  eventSource.onmessage = (e) => {
    if (!liveEnabled) return;
    try {
      const event = JSON.parse(e.data);
      if (!event || !event.id) return;

      // Avoid duplicates
      if (allEvents.some(ev => ev.id === event.id)) return;
      allEvents.push(event);

      // Check if this event passes current filters
      const passes = eventPassesFilters(event);
      if (passes) {
        filteredEvents.push(event);
        appendEventRow(event);
      }
    } catch (err) {
      console.warn('Failed to parse SSE event:', err, e.data);
    }
  };

  eventSource.addEventListener('close', () => {
    setConnectionStatus('disconnected');
    scheduleReconnect();
  });

  eventSource.onerror = () => {
    setConnectionStatus('disconnected');
    eventSource.close();
    eventSource = null;
    scheduleReconnect();
  };
}

/**
 * Check if a single event passes the active filters.
 * @param {Object} event
 * @returns {boolean}
 */
function eventPassesFilters(event) {
  const { eventType, processName, pathContains, search } = activeFilters;
  if (eventType && event.event_type !== eventType) return false;
  if (processName && !(event.process_name || '').toLowerCase().includes(processName.toLowerCase())) return false;
  if (pathContains && !(event.path || '').toLowerCase().includes(pathContains.toLowerCase())) return false;
  if (search) {
    const searchLower = search.toLowerCase();
    const haystack = [
      event.path || '',
      event.command_line || '',
      event.remote_host || '',
      event.process_name || '',
      event.event_type || '',
      JSON.stringify(event.metadata || {}),
    ].join(' ').toLowerCase();
    if (!haystack.includes(searchLower)) return false;
  }
  return true;
}

/**
 * Schedule a reconnect attempt after a delay.
 */
function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectSSE();
  }, RECONNECT_DELAY_MS);
}

/**
 * Disconnect the SSE stream.
 */
function disconnectSSE() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  setConnectionStatus('disconnected');
}

// ---------------------------------------------------------------------------
// UI event handlers
// ---------------------------------------------------------------------------

/**
 * Read current filter inputs and update activeFilters.
 */
function readFilters() {
  activeFilters.eventType = document.getElementById('filter-event-type')?.value || '';
  activeFilters.processName = document.getElementById('filter-process')?.value.trim() || '';
  activeFilters.pathContains = document.getElementById('filter-path')?.value.trim() || '';
  activeFilters.search = document.getElementById('filter-search')?.value.trim() || '';
}

/**
 * Clear all filter inputs and reset the active filters.
 */
function clearFilters() {
  const fields = ['filter-event-type', 'filter-process', 'filter-path', 'filter-search'];
  fields.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  activeFilters = { eventType: '', processName: '', pathContains: '', search: '' };
  applyFilters();
}

/**
 * Clear the event display without removing stored events.
 */
function clearDisplay() {
  const rows = elEventList?.querySelectorAll('.event-row');
  rows?.forEach(row => row.remove());
  if (elDisplayCount) elDisplayCount.textContent = '0';
  if (elEmptyState) elEmptyState.style.display = 'flex';
  closeDetailPanel();
}

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------

/**
 * Set up all DOM references and event listeners, then start the SSE connection.
 */
function init() {
  elEventList = document.getElementById('event-list');
  elEmptyState = document.getElementById('empty-state');
  elStatusDot = document.getElementById('status-dot');
  elStatusText = document.getElementById('status-text');
  elSubscriberCount = document.getElementById('subscriber-count');
  elDisplayCount = document.getElementById('display-count');
  elDetailPanel = document.getElementById('detail-panel');
  elDetailContent = document.getElementById('detail-content');

  // Filter buttons
  document.getElementById('btn-apply-filters')?.addEventListener('click', () => {
    readFilters();
    applyFilters();
  });

  document.getElementById('btn-clear-filters')?.addEventListener('click', () => {
    clearFilters();
  });

  // Live filter inputs on Enter
  ['filter-event-type', 'filter-process', 'filter-path', 'filter-search'].forEach(id => {
    const el = document.getElementById(id);
    el?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        readFilters();
        applyFilters();
      }
    });
  });

  // Stream controls
  document.getElementById('toggle-live')?.addEventListener('change', (e) => {
    liveEnabled = e.target.checked;
    if (liveEnabled) {
      connectSSE();
    } else {
      disconnectSSE();
    }
  });

  document.getElementById('toggle-autoscroll')?.addEventListener('change', (e) => {
    autoScrollEnabled = e.target.checked;
  });

  document.getElementById('btn-clear-display')?.addEventListener('click', clearDisplay);

  // History / stats
  document.getElementById('btn-load-history')?.addEventListener('click', loadHistory);
  document.getElementById('btn-refresh-stats')?.addEventListener('click', loadStats);

  // Detail panel close
  document.getElementById('btn-close-detail')?.addEventListener('click', closeDetailPanel);

  // Keyboard shortcut: Escape to close detail panel
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDetailPanel();
  });

  // Start SSE connection
  connectSSE();

  // Periodic stats refresh
  statsRefreshTimer = setInterval(loadStats, STATS_REFRESH_INTERVAL_MS);

  // Load initial history
  setTimeout(loadHistory, 500);
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
