import React, { useState, useEffect } from 'react';
import { getAgent, updateAgentMission } from '../api';

function MissionPanel({ agentId }) {
  const [missionText, setMissionText] = useState('');
  const [activeGoalId, setActiveGoalId] = useState('');
  const [goals, setGoals] = useState([]);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (agentId) {
      fetchAgent();
      fetchGoals();
    }
  }, [agentId]);

  async function fetchAgent() {
    try {
      const agent = await getAgent(agentId);
      setMissionText(agent.mission || '');
      setActiveGoalId(agent.active_goal_id || '');
    } catch (err) {
      setError('Failed to load agent');
    }
  }

  async function fetchGoals() {
    try {
      const data = await getGoals();
      setGoals(data);
    } catch (err) {
      // ignore
    }
  }

  async function handleSave() {
    try {
      await updateAgentMission(agentId, {
        mission: missionText,
        active_goal_id: activeGoalId || null,
      });
      setEditing(false);
    } catch (err) {
      setError('Failed to update mission');
    }
  }

  return (
    <div className="mission-panel">
      <h3>Mission</h3>
      {error && <div className="error">{error}</div>}
      {!editing ? (
        <div className="mission-display">
          <p>{missionText || 'No mission set.'}</p>
          <p>Active Goal: {goals.find(g => g.id === activeGoalId)?.title || 'None'}</p>
          <button onClick={() => setEditing(true)}>Edit Mission</button>
        </div>
      ) : (
        <div className="mission-edit">
          <textarea
            value={missionText}
            onChange={e => setMissionText(e.target.value)}
            placeholder="Enter mission text"
          />
          <select
            value={activeGoalId}
            onChange={e => setActiveGoalId(e.target.value)}
          >
            <option value="">No goal</option>
            {goals.map(goal => (
              <option key={goal.id} value={goal.id}>{goal.title}</option>
            ))}
          </select>
          <div className="edit-actions">
            <button onClick={handleSave}>Save</button>
            <button onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

export default MissionPanel;