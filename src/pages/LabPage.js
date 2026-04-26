import React, { useEffect, useState } from 'react';
import api from '../utils/api';

function LabPage() {
  const [improvements, setImprovements] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchImprovements();
  }, []);

  const fetchImprovements = async () => {
    try {
      const response = await api.get('/v1/improvements');
      setImprovements(response.data);
    } catch (err) {
      console.error('Failed to fetch improvements:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (id) => {
    try {
      await api.post(`/v1/improvements/${id}/approve`);
      fetchImprovements();
    } catch (err) {
      console.error('Failed to approve improvement:', err);
    }
  };

  const handleReject = async (id) => {
    try {
      await api.post(`/v1/improvements/${id}/reject`);
      fetchImprovements();
    } catch (err) {
      console.error('Failed to reject improvement:', err);
    }
  };

  const handleConvert = async (id) => {
    try {
      await api.post(`/v1/improvements/${id}/convert`);
      fetchImprovements();
    } catch (err) {
      console.error('Failed to convert improvement:', err);
    }
  };

  if (loading) return <div className="loading">Loading lab...</div>;

  return (
    <div className="page lab-page">
      <h1>Improvement Proposals</h1>
      {improvements.length === 0 ? (
        <p>No improvement proposals yet.</p>
      ) : (
        <div className="improvements-list">
          {improvements.map((imp) => (
            <div key={imp.id} className="card">
              <div className="card-header">
                <span className="status-pill">{imp.status}</span>
                <span className="proposal-title">{imp.title}</span>
              </div>
              <div className="card-body">
                <p>{imp.description}</p>
                <p><strong>Submitted by:</strong> {imp.submitter}</p>
              </div>
              <div className="card-actions">
                <button
                  className="btn btn-success"
                  onClick={() => handleApprove(imp.id)}
                  disabled={imp.status !== 'pending'}
                >
                  Approve
                </button>
                <button
                  className="btn btn-danger"
                  onClick={() => handleReject(imp.id)}
                  disabled={imp.status !== 'pending'}
                >
                  Reject
                </button>
                <button
                  className="btn btn-warning"
                  onClick={() => handleConvert(imp.id)}
                  disabled={imp.status !== 'approved'}
                >
                  Convert to Goal
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default LabPage;