import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import api from '../utils/api';

function AgentProfilePage() {
  const { id } = useParams();
  const [agent, setAgent] = useState(null);
  const [missionText, setMissionText] = useState('');
  const [activeGoalId, setActiveGoalId] = useState('');
  const [goals, setGoals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetchAgent();
    fetchGoals();
  }, [id]);

  const fetchAgent = async () => {
    try {
      const response = await api.get(`/v1/agents/${id}`);
      setAgent(response);
      setMissionText(response.mission || '');
      setActiveGoalId(response.active_goal_id || '');
    } catch (err) {
      console.error('Failed to fetch agent:', err);
    } finally {
      setLoading(false);
    }
  };

  const fetchGoals = async () => {
    try {
      const response = await api.get('/v1/goals');
      setGoals(response);
    } catch (err) {
      console.error('Failed to fetch goals:', err);
    }
  };

  const handleSaveMission = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      await api.put(`/v1/agents/${id}/mission`, {
        mission: missionText,
        active_goal_id: activeGoalId,
      });
      // Update local state
      setAgent(prev => ({ ...prev, mission: missionText, active_goal_id: activeGoalId }));
    } catch (err) {
      console.error('Failed to update mission:', err);
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="loading">Loading agent...</div>;
  if (!agent) return <div className="error">Agent not found.</div>;

  return (
    <div className="page agent-profile-page">
      <h1>{agent.name}</h1>
      <div className="agent-details card">
        <p><strong>ID:</strong> {agent.id}</p>
        <p><strong>Type:</strong> {agent.type}</p>
        <p><strong>Status:</strong> <span className="status-pill">{agent.status}</span></p>
        <p><strong>Owner:</strong> {agent.owner}</p>
      </div>

      <div className="card mission-panel">
        <h2>Mission</h2>
        <form onSubmit={handleSaveMission} className="mission-form">
          <label>
            Mission Text:
            <textarea
              value={missionText}
              onChange={(e) => setMissionText(e.target.value)}
            />
          </label>
          <label>
            Active Goal:
            <select
              value={activeGoalId}
              onChange={(e) => setActiveGoalId(e.target.value)}
            >
              <option value="">None</option>
              {goals.map((goal) => (
                <option key={goal.id} value={goal.id}>{goal.description}</option>
              ))}
            </select>
          </label>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? 'Saving...' : 'Save Mission'}
          </button>
        </form>
      </div>

      {/* existing agent details sections can go here */}
    </div>
  );
}

export default AgentProfilePage;