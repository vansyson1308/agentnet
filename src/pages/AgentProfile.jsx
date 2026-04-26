import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { getAgent, updateMission } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import Card from '../components/Card';
import { toast } from 'react-hot-toast';

const AgentProfile = () => {
  const { id } = useParams();
  const { token } = useAuth();
  const [agent, setAgent] = useState(null);
  const [loading, setLoading] = useState(true);
  const [missionText, setMissionText] = useState('');
  const [activeGoalId, setActiveGoalId] = useState('');

  useEffect(() => {
    if (token && id) {
      fetchAgent();
    }
  }, [token, id]);

  const fetchAgent = async () => {
    try {
      const data = await getAgent(token, id);
      setAgent(data);
      setMissionText(data.mission || '');
      setActiveGoalId(data.active_goal_id || '');
    } catch (err) {
      toast.error('Failed to load agent');
    } finally {
      setLoading(false);
    }
  };

  const handleSaveMission = async () => {
    try {
      await updateMission(token, id, { mission: missionText, active_goal_id: activeGoalId || null });
      toast.success('Mission updated');
    } catch (err) {
      toast.error('Failed to update mission');
    }
  };

  if (loading) return <div className="text-center p-8"><span className="loading loading-spinner loading-lg"></span></div>;
  if (!agent) return <div className="text-center p-8">Agent not found</div>;

  return (
    <div className="container mx-auto p-4 space-y-6">
      <h1 className="text-2xl font-bold">{agent.name}</h1>
      <p className="text-gray-600">{agent.description}</p>

      {/* Mission Panel */}
      <Card title="Mission">
        <div className="space-y-2">
          <textarea
            className="textarea textarea-bordered w-full"
            value={missionText}
            onChange={(e) => setMissionText(e.target.value)}
            placeholder="Enter mission text..."
          />
          <div>
            <label className="label">Active Goal ID</label>
            <input
              type="text"
              className="input input-bordered w-full"
              value={activeGoalId}
              onChange={(e) => setActiveGoalId(e.target.value)}
              placeholder="Optional"
            />
          </div>
          <button className="btn btn-primary" onClick={handleSaveMission}>Save Mission</button>
        </div>
      </Card>

      {/* Add other profile details here */}
    </div>
  );
};

export default AgentProfile;