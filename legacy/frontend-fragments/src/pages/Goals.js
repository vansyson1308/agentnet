import React, { useState, useEffect } from 'react';
import { api } from '../api';
import { Cards, StatusPill } from '../components';

const Goals = () => {
  const [goals, setGoals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [newGoal, setNewGoal] = useState({ title: '', description: '', priority: 'medium' });
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    fetchGoals();
  }, []);

  const fetchGoals = async () => {
    try {
      setLoading(true);
      const response = await api.get('/v1/goals');
      setGoals(response.data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleCreate = async (e) => {
    e.preventDefault();
    if (!newGoal.title.trim()) return;
    try {
      setSubmitting(true);
      await api.post('/v1/goals', newGoal);
      setNewGoal({ title: '', description: '', priority: 'medium' });
      await fetchGoals();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return <div className="loading">Loading goals...</div>;
  if (error) return <div className="error">Error: {error}</div>;

  return (
    <div className="goals-page">
      <h1>Society Goal Map</h1>

      <div className="create-goal-section">
        <h2>Create New Goal</h2>
        <form onSubmit={handleCreate} className="goal-form">
          <input
            type="text"
            placeholder="Title"
            value={newGoal.title}
            onChange={(e) => setNewGoal({ ...newGoal, title: e.target.value })}
            required
          />
          <textarea
            placeholder="Description"
            value={newGoal.description}
            onChange={(e) => setNewGoal({ ...newGoal, description: e.target.value })}
          />
          <select
            value={newGoal.priority}
            onChange={(e) => setNewGoal({ ...newGoal, priority: e.target.value })}
          >
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
          <button type="submit" disabled={submitting || !newGoal.title.trim()}>
            {submitting ? 'Creating...' : 'Create Goal'}
          </button>
        </form>
      </div>

      <div className="goal-list">
        {goals.length === 0 ? (
          <p>No goals found. Create your first goal above.</p>
        ) : (
          goals.map((goal) => (
            <div key={goal.id} className="goal-card">
              <Cards>
                <h3>{goal.title}</h3>
                <p>{goal.description}</p>
                <StatusPill status={goal.priority} />
                <small>Depth: {goal.depth} | Parent: {goal.parent_id || 'None'}</small>
              </Cards>
            </div>
          ))
        )}
      </div>
    </div>
  );
};

export default Goals;