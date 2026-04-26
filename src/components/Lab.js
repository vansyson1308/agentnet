import React, { useState, useEffect } from 'react';
import api from '../api/client';

const Lab = () => {
  const [improvements, setImprovements] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchImprovements = async () => {
    try {
      setLoading(true);
      const data = await api.get('/v1/improvements');
      setImprovements(data);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch improvements');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchImprovements();
  }, []);

  const handleApprove = async (id) => {
    try {
      await api.post(`/v1/improvements/${id}/approve`);
      fetchImprovements();
    } catch (err) {
      setError(err.message || 'Failed to approve');
    }
  };

  const handleReject = async (id) => {
    try {
      await api.post(`/v1/improvements/${id}/reject`);
      fetchImprovements();
    } catch (err) {
      setError(err.message || 'Failed to reject');
    }
  };

  const handleConvert = async (id) => {
    try {
      await api.post(`/v1/improvements/${id}/convert`);
      fetchImprovements();
    } catch (err) {
      setError(err.message || 'Failed to convert');
    }
  };

  if (loading) return <div>Loading lab proposals...</div>;
  if (error) return <div className="error">{error}</div>;

  return (
    <div className="lab-tab">
      <h2>Improvement Proposals</h2>
      <div className="proposal-list">
        {improvements.map((item) => (
          <div key={item.id} className="card">
            <h3>{item.title}</h3>
            <p>{item.description}</p>
            <span className="status-pill">{item.status}</span>
            <div className="actions">
              <button onClick={() => handleApprove(item.id)}>Approve</button>
              <button onClick={() => handleReject(item.id)}>Reject</button>
              <button onClick={() => handleConvert(item.id)}>Convert</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default Lab;