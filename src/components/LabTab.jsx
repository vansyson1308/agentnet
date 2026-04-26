import React, { useState, useEffect } from 'react';
import { getImprovements, approveImprovement, rejectImprovement, convertImprovement } from '../api';

function LabTab() {
  const [improvements, setImprovements] = useState([]);
  const [error, setError] = useState('');

  useEffect(() => {
    fetchImprovements();
  }, []);

  async function fetchImprovements() {
    try {
      const data = await getImprovements();
      setImprovements(data);
    } catch (err) {
      setError('Failed to load improvements');
    }
  }

  async function handleApprove(id) {
    try {
      await approveImprovement(id);
      fetchImprovements();
    } catch (err) {
      setError('Failed to approve');
    }
  }

  async function handleReject(id) {
    try {
      await rejectImprovement(id);
      fetchImprovements();
    } catch (err) {
      setError('Failed to reject');
    }
  }

  async function handleConvert(id) {
    try {
      await convertImprovement(id);
      fetchImprovements();
    } catch (err) {
      setError('Failed to convert');
    }
  }

  return (
    <div className="tab-content">
      <h2>Improvement Proposals (Lab)</h2>
      {error && <div className="error">{error}</div>}
      {improvements.length === 0 ? (
        <p>No improvement proposals pending.</p>
      ) : (
        improvements.map(improvement => (
          <div key={improvement.id} className="card">
            <h3>{improvement.title}</h3>
            <p>{improvement.description}</p>
            <span className="status-pill">Status: {improvement.status}</span>
            <div className="action-buttons">
              <button className="approve" onClick={() => handleApprove(improvement.id)}>Approve</button>
              <button className="reject" onClick={() => handleReject(improvement.id)}>Reject</button>
              <button className="convert" onClick={() => handleConvert(improvement.id)}>Convert to Goal</button>
            </div>
          </div>
        ))
      )}
    </div>
  );
}

export default LabTab;