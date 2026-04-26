import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import api from '../api/client';
import AgentMissionEditor from '../components/AgentMissionEditor';

const AgentProfile = () => {
  const { id } = useParams();
  const [agent, setAgent] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchAgent = async () => {
      try {
        setLoading(true);
        const data = await api.get(`/v1/agents/${id}`);
        setAgent(data);
        setError(null);
      } catch (err) {
        setError(err.message || 'Failed to fetch agent');
      } finally {
        setLoading(false);
      }
    };
    fetchAgent();
  }, [id]);

  const handleMissionUpdate = (updatedFields) => {
    setAgent((prev) => ({
      ...prev,
      mission: updatedFields.mission,
      active_goal_id: updatedFields.goalId,
    }));
  };

  if (loading) return <div>Loading agent profile...</div>;
  if (error) return <div className="error">{error}</div>;
  if (!agent) return <div>Agent not found</div>;

  return (
    <div className="agent-profile-page">
      <div className="agent-info card">
        <h2>{agent.name}</h2>
        <p>ID: {agent.id}</p>
        <p>Status: <span className="status-pill">{agent.status}</span></p>
        <p>Owner: {agent.owner}</p>
        <p>Description: {agent.description}</p>
      </div>
      <AgentMissionEditor agentId={agent.id} onMissionUpdate={handleMissionUpdate} />
    </div>
  );
};

export default AgentProfile;