import React, { useState, useEffect } from 'react';
import { getImprovements, approveImprovement, rejectImprovement, convertImprovement } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import Card from '../components/Card';
import StatusPill from '../components/StatusPill';
import { toast } from 'react-hot-toast';

const Lab = () => {
  const { token } = useAuth();
  const [improvements, setImprovements] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (token) {
      fetchImprovements();
    }
  }, [token]);

  const fetchImprovements = async () => {
    try {
      const data = await getImprovements(token);
      setImprovements(data);
    } catch (err) {
      toast.error('Failed to load improvements');
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (id) => {
    try {
      await approveImprovement(token, id);
      toast.success('Improvement approved');
      fetchImprovements();
    } catch (err) {
      toast.error('Failed to approve');
    }
  };

  const handleReject = async (id) => {
    try {
      await rejectImprovement(token, id);
      toast.success('Improvement rejected');
      fetchImprovements();
    } catch (err) {
      toast.error('Failed to reject');
    }
  };

  const handleConvert = async (id) => {
    try {
      await convertImprovement(token, id);
      toast.success('Improvement converted to agent');
      fetchImprovements();
    } catch (err) {
      toast.error('Failed to convert');
    }
  };

  if (loading) return <div className="text-center p-8"><span className="loading loading-spinner loading-lg"></span></div>;

  return (
    <div className="container mx-auto p-4 space-y-4">
      <h1 className="text-2xl font-bold">Improvement Proposals (Lab)</h1>
      {improvements.length === 0 && <p className="text-gray-500">No proposals yet.</p>}
      {improvements.map((imp) => (
        <Card key={imp.id} title={imp.title}>
          <p>{imp.description}</p>
          <div className="mt-2">
            <StatusPill status={imp.status} />
          </div>
          {imp.status === 'pending' && (
            <div className="mt-4 flex gap-2">
              <button className="btn btn-success btn-sm" onClick={() => handleApprove(imp.id)}>Approve</button>
              <button className="btn btn-error btn-sm" onClick={() => handleReject(imp.id)}>Reject</button>
              <button className="btn btn-info btn-sm" onClick={() => handleConvert(imp.id)}>Convert to Agent</button>
            </div>
          )}
        </Card>
      ))}
    </div>
  );
};

export default Lab;