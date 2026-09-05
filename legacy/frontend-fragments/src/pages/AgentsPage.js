import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../utils/api';

function AgentsPage() {
  const [agents, setAgents] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchAgents();
  }, []);

  const fetchAgents = async () => {
    try {
      const response = await api.get('/v1/agents');
      setAgents(response);
    } catch (err) {
      console.error('Failed to fetch agents:', err);
    } finally {
      setLoading(false);
    }
  };

  if (loading) return <div className="loading">Loading agents...</div>;

  return (
    <div className="page agents-page">
      <h1>Agents</h1>
      {agents.length === 0 ? (
        <p>No agents yet.</p>
      ) : (
        <div className="agent-list">
          {agents.map((agent) => (
            <Link key={agent.id} to={`/agents/${agent.id}`} className="card agent-card">
              <h3>{agent.name}</h3>
              <p>{agent.type}</p>
              <span className="status-pill">{agent.status}</span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

export default AgentsPage;