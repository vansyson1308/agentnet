import React, { useState, useEffect } from 'react';
import { getGoals, createGoal } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import Card from '../components/Card';
import StatusPill from '../components/StatusPill';
import { toast } from 'react-hot-toast';

const Goals = () => {
  const { token } = useAuth();
  const [goals, setGoals] = useState([]);
  const [loading, setLoading] = useState(true);

  // Create goal form state
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [parentId, setParentId] = useState('');

  useEffect(() => {
    if (token) {
      fetchGoals();
    }
  }, [token]);

  const fetchGoals = async () => {
    try {
      const data = await getGoals(token);
      setGoals(data);
    } catch (err) {
      toast.error('Failed to load goals');
    } finally {
      setLoading(false);
    }
  };

  const handleCreateGoal = async (e) => {
    e.preventDefault();
    try {
      await createGoal(token, { title, description, parent_id: parentId || null });
      toast.success('Goal created');
      setTitle('');
      setDescription('');
      setParentId('');
      fetchGoals();
    } catch (err) {
      toast.error('Failed to create goal');
    }
  };

  if (loading) return <div className="text-center p-8"><span className="loading loading-spinner loading-lg"></span></div>;

  return (
    <div className="container mx-auto p-4 space-y-6">
      <h1 className="text-2xl font-bold">Society Goal Map</h1>

      {/* Create Goal Form */}
      <Card title="Create New Goal">
        <form onSubmit={handleCreateGoal} className="space-y-4">
          <div>
            <label className="label">Title</label>
            <input type="text" className="input input-bordered w-full" value={title} onChange={(e) => setTitle(e.target.value)} required />
          </div>
          <div>
            <label className="label">Description</label>
            <textarea className="textarea textarea-bordered w-full" value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div>
            <label className="label">Parent Goal ID (optional)</label>
            <input type="text" className="input input-bordered w-full" value={parentId} onChange={(e) => setParentId(e.target.value)} />
          </div>
          <button type="submit" className="btn btn-primary">Create Goal</button>
        </form>
      </Card>

      {/* Goal List */}
      <div className="grid gap-4">
        {goals.length === 0 && <p className="text-gray-500">No goals yet.</p>}
        {goals.map((goal) => (
          <Card key={goal.id} title={goal.title}>
            <p>{goal.description}</p>
            <div className="flex items-center gap-2 mt-2">
              <StatusPill status={goal.status} />
              {goal.parent && <span className="text-sm text-gray-500">Parent: {goal.parent.title}</span>}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
};

export default Goals;