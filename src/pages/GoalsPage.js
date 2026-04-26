import React, { useEffect, useState } from 'react';
import api from '../utils/api';

function GoalsPage() {
  const [goals, setGoals] = useState([]);
  const [newGoal, setNewGoal] = useState({ description: '', priority: 'medium' });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchGoals();
  }, []);

  const fetchGoals = async () => {
    try {
      const response = await api.get('/v1/goals');
      setGoals(response.data);
    } catch (err) {
      console.error('Failed to fetch goals:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleCreateGoal = async (e) => {
    e.preventDefault();
    try {
      await api.post('/v1/goals', newGoal);
      setNewGoal({ description: '', priority: 'medium' });
      fetchGoals();
    } catch (err) {
      console.error('Failed to create goal:', err);
    }
  };

  if (loading) return <div className="loading">Loading goals...</div>;

  return (
    <div className="page goals-page">
      <h1>Society Goals</h1>
      <div className="card">
        <h2>Create New Goal</h2>
        <form onSubmit={handleCreateGoal} className="goal-form">
          <label>
            Description:
            <textarea
              value={newGoal.description}
              onChange={(e) => setNewGoal({ ...newGoal, description: e.target.value })}
              required
            />
          </label>
          <label>
            Priority:
            <select
              value={newGoal.priority}
              onChange={(e) => setNewGoal({ ...newGoal, priority: e.target.value })}
            >
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </label>
          <button type="submit" className="btn btn-primary">Create Goal</button>
        </form>
      </div>
      <div className="goal-map">
        <h2>Goal Map</h2>
        {goals.length === 0 ? (
          <p>No goals defined yet.</p>
        ) : (
          <div className="goal-list">
            {goals.map((goal) => (
              <div key={goal.id} className="card status-pill">
                <div className="goal-priority">{goal.priority}</div>
                <div className="goal-description">{goal.description}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default GoalsPage;