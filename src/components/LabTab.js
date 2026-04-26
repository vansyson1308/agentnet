import React, { useState, useEffect } from 'react';
import apiClient from '../api/client';
import StatusPill from './StatusPill';

const LabTab = () => {
  const [proposals, setProposals] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchProposals();
  }, []);

  const fetchProposals = async () => {
    try {
      const res = await apiClient.get('/v1/improvements');
      setProposals(res.data);
    } catch (err) {
      console.error('Failed to load proposals', err);
    } finally {
      setLoading(false);
    }
  };

  const handleAction = async (id, action) => {
    // action: 'approve', 'reject', 'convert'
    try {
      await apiClient.post(`/v1/improvements/${id}/${action}`);
      fetchProposals(); // refresh
    } catch (err) {
      console.error(`Failed to ${action} proposal`, err);
    }
  };

  if (loading) return <div className="loading-spinner">Loading proposals...</div>;

  return (
    <div className="lab-tab">
      <h2>Improvement Proposals</h2>
      <div className="proposals-list">
        {proposals.length === 0 && <p>No proposals yet.</p>}
        {proposals.map((proposal) => (
          <div key={proposal.id} className="card">
            <div className="card-header">
              <h3>{proposal.title}</h3>
              <StatusPill status={proposal.status} />
            </div>
            <p className="card-description">{proposal.description}</p>
            <div className="proposal-actions">
              <button
                className="btn-approve"
                onClick={() => handleAction(proposal.id, 'approve')}
                disabled={proposal.status !== 'pending'}
              >
                Approve
              </button>
              <button
                className="btn-reject"
                onClick={() => handleAction(proposal.id, 'reject')}
                disabled={proposal.status !== 'pending'}
              >
                Reject
              </button>
              <button
                className="btn-convert"
                onClick={() => handleAction(proposal.id, 'convert')}
                disabled={proposal.status !== 'approved'}
              >
                Convert to Goal
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default LabTab;