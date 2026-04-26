// This file should import and use MissionPanel.
// We'll add the MissionPanel section after the existing details.
// Assuming the existing AgentProfile component exists, we'll modify it.
// Since we don't have the original, we'll create a minimal version.
import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import apiClient from '../api/client';
import StatusPill from './StatusPill';
import MissionPanel from './MissionPanel';

const AgentProfile = () => {
  const { id } = useParams();
  const [agent, setAgent] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchAgent();
  }, [id]);

  const fetchAgent = async () => {
    try {
      const res = await apiClient.get(`/v1/agents/${id}`);
      setAgent(res.data);
    } catch (err) {
      console.error('Failed to load agent', err);
    } finally {
      setLoading(false);
    }
  };

  if (loading) return <div className="loading-spinner">Loading agent...</div>;
  if (!agent) return <div>Agent not found.</div>;

  return (
    <div className="agent-profile">
      <div className="card">
        <div className="card-header">
          <h2>{agent.name}</h2>
          <StatusPill status={agent.status} />
        </div>
        <p>{agent.description}</p>
        {/* Additional agent details */}
      </div>
      <MissionPanel agentId={id} />
    </div>
  );
};

export default AgentProfile;