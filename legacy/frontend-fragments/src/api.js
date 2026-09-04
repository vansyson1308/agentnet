// Existing API client (assume setup with JWT token)
const BASE_URL = process.env.REACT_APP_API_URL || '/api/v1';

async function apiCall(path, options = {}) {
  const token = localStorage.getItem('jwt');
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...options.headers,
  };
  const response = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers,
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || `Request failed: ${response.status}`);
  }
  return response.json();
}

// Goals
export async function getGoals() {
  return apiCall('/v1/goals');
}

export async function createGoal(data) {
  return apiCall('/v1/goals', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

// Improvements
export async function getImprovements() {
  return apiCall('/v1/improvements');
}

export async function approveImprovement(id) {
  return apiCall(`/v1/improvements/${id}/approve`, { method: 'POST' });
}

export async function rejectImprovement(id) {
  return apiCall(`/v1/improvements/${id}/reject`, { method: 'POST' });
}

export async function convertImprovement(id) {
  return apiCall(`/v1/improvements/${id}/convert`, { method: 'POST' });
}

// Memory
export async function getMemory(tagFilter = '') {
  const params = tagFilter ? `?tag=${encodeURIComponent(tagFilter)}` : '';
  return apiCall(`/v1/memory${params}`);
}

// Agent (mission update)
export async function getAgent(agentId) {
  return apiCall(`/v1/agents/${agentId}`);
}

export async function updateAgentMission(agentId, data) {
  return apiCall(`/v1/agents/${agentId}/mission`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

// Export existing functions (assume they exist)
export async function getAgents() { ... }
export async function getOffers() { ... }
// ... (other existing functions)