import React, { useState, useEffect } from 'react';
import apiClient from '../api/client';
import StatusPill from './StatusPill';

const GoalsTab = () => {
  const [goals, setGoals] = useState([]);
  const [loading, setLoading] = useState(true);

  // New goal form state
  const [newTitle, setNewTitle] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    fetchGoals();
  }, []);

  const fetchGoals = async () => {
    try {
      const res = await apiClient.get('/v1/goals');
      setGoals(res.data);
    } catch (err) {
      console.error('Failed to load goals', err);
    } finally {
      setLoading(false);
    }
  };

  const handleCreateGoal = async (e) => {
    e.preventDefault();
    if (!newTitle.trim()) return;
    setSubmitting(true);
    try {
      await apiClient.post('/v1/goals', {
        title: newTitle,
        description: newDescription,
      });
      setNewTitle('');
      setNewDescription('');
      fetchGoals(); // refresh
    } catch (err) {
      console.error('Failed to create goal', err);
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return <div className="loading-spinner">Loading goals...</div>;

  return (
    <div className="goals-tab">
      <h2>Society Goal Map</h2>
      <div className="goals-list">
        {goals.length === 0 && <p>No goals defined yet.</p>}
        {goals.map((goal) => (
          <div key={goal.id} className="card">
            <div className="card-header">
              <h3>{goal.title}</h3>
              <StatusPill status={goal.status || 'active'} />
            </div>
            <p className="card-description">{goal.description}</p>
          </div>
        ))}
      </div>

      <h3>Create New Goal</h3>
      <form onSubmit={handleCreateGoal} className="goals-form">
        <input
          type="text"
          placeholder="Goal title"
          value={newTitle}
          onChange={(e) => setNewTitle(e.target.value)}
          required
        />
        <textarea
          placeholder="Description (optional)"
          value={newDescription}
          onChange={(e) => setNewDescription(e.target.value)}
        />
        <button type="submit" disabled={submitting}>
          {submitting ? 'Creating...' : 'Create Goal'}
        </button>
      </form>
    </div>
  );
};

export default GoalsTab;