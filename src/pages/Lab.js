import React, { useState, useEffect } from 'react';
import { api } from '../api';
import { Cards, StatusPill, Button } from '../components';

const Lab = () => {
  const [proposals, setProposals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchProposals();
  }, []);

  const fetchProposals = async () => {
    try {
      setLoading(true);
      const response = await api.get('/v1/improvements');
      setProposals(response.data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleAction = async (id, action) => {
    try {
      await api.put(`/v1/improvements/${id}`, { action }); // action: approve | reject | convert
      await fetchProposals();
    } catch (err) {
      setError(err.message);
    }
  };

  if (loading) return <div className="loading">Loading lab proposals...</div>;
  if (error) return <div className="error">Error: {error}</div>;

  return (
    <div className="lab-page">
      <h1>Improvement Lab</h1>
      {proposals.length === 0 ? (
        <p>No improvement proposals yet.</p>
      ) : (
        <div className="proposal-list">
          {proposals.map((prop) => (
            <div key={prop.id} className="proposal-card">
              <Cards>
                <div className="proposal-header">
                  <h3>{prop.title}</h3>
                  <StatusPill status={prop.status} />
                </div>
                <p>{prop.description}</p>
                <div className="proposal-actions">
                  <Button onClick={() => handleAction(prop.id, 'approve')} variant="success">Approve</Button>
                  <Button onClick={() => handleAction(prop.id, 'reject')} variant="danger">Reject</Button>
                  <Button onClick={() => handleAction(prop.id, 'convert')} variant="info">Convert to Goal</Button>
                </div>
              </Cards>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default Lab;