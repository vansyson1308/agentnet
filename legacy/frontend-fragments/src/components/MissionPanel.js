import React, { useState, useEffect } from 'react';
import apiClient from '../api/client';

const MissionPanel = ({ agentId }) => {
  const [agent, setAgent] = useState(null);
  const [missionText, setMissionText] = useState('');
  const [activeGoalId, setActiveGoalId] = useState('');
  const [goals, setGoals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetchAgentAndGoals();
  }, [agentId]);

  const fetchAgentAndGoals = async () => {
    try {
      const [agentRes, goalsRes] = await Promise.all([
        apiClient.get(`/v1/agents/${agentId}`),
        apiClient.get('/v1/goals'),
      ]);
      const agentData = agentRes.data;
      setAgent(agentData);
      setMissionText(agentData.mission_text || '');
      setActiveGoalId(agentData.active_goal_id || '');
      setGoals(goalsRes.data);
    } catch (err) {
      console.error('Failed to load agent/goals', err);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await apiClient.patch(`/v1/agents/${agentId}`, {
        mission_text: missionText,
        active_goal_id: activeGoalId || null,
      });
      // Optionally show success
    } catch (err) {
      console.error('Failed to update mission', err);
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="loading-spinner">Loading...</div>;

  return (
    <div className="mission-panel card">
      <h3>Mission</h3>
      <div className="mission-text">
        <textarea
          placeholder="Define the agent's mission..."
          value={missionText}
          onChange={(e) => setMissionText(e.target.value)}
          rows="4"
        />
      </div>
      <div className="active-goal">
        <label>Active Goal:</label>
        <select
          value={activeGoalId}
          onChange={(e) => setActiveGoalId(e.target.value)}
        >
          <option value="">None</option>
          {goals.map((goal) => (
            <option key={goal.id} value={goal.id}>
              {goal.title}
            </option>
          ))}
        </select>
      </div>
      <button
        className="btn-primary"
        onClick={handleSave}
        disabled={saving}
      >
        {saving ? 'Saving...' : 'Save Mission'}
      </button>
    </div>
  );
};

export default MissionPanel;