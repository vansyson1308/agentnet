import React, { useState, useEffect } from 'react';
import api from '../api/client';

const AgentMissionEditor = ({ agentId, onMissionUpdate }) => {
  const [mission, setMission] = useState('');
  const [goalId, setGoalId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(false);

  const fetchCurrentMission = async () => {
    try {
      const data = await api.get(`/v1/agents/${agentId}`);
      setMission(data.mission || '');
      setGoalId(data.active_goal_id || '');
    } catch (err) {
      setError(err.message || 'Failed to load agent data');
    }
  };

  useEffect(() => {
    if (agentId) {
      fetchCurrentMission();
    }
  }, [agentId]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setSuccess(false);
    try {
      await api.patch(`/v1/agents/${agentId}`, { mission, active_goal_id: goalId });
      setSuccess(true);
      if (onMissionUpdate) onMissionUpdate({ mission, goalId });
    } catch (err) {
      setError(err.message || 'Failed to update mission');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mission-editor card">
      <h3>Edit Mission</h3>
      <form onSubmit={handleSubmit}>
        <label>
          Mission Text:
          <textarea
            value={mission}
            onChange={(e) => setMission(e.target.value)}
            rows={4}
            placeholder="Enter mission statement"
          />
        </label>
        <label>
          Active Goal ID:
          <input
            type="text"
            value={goalId}
            onChange={(e) => setGoalId(e.target.value)}
            placeholder="Goal ID (optional)"
          />
        </label>
        <button type="submit" disabled={loading}>
          {loading ? 'Saving...' : 'Save'}
        </button>
        {success && <span className="success-message">Mission updated!</span>}
        {error && <span className="error-message">{error}</span>}
      </form>
    </div>
  );
};

export default AgentMissionEditor;