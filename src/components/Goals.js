import React, { useState, useEffect } from 'react';
import api from '../api/client';

const Goals = () => {
  const [goals, setGoals] = useState([]);
  const [newGoal, setNewGoal] = useState({ title: '', description: '', status: 'active', parent_id: '' });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchGoals = async () => {
    try {
      setLoading(true);
      const data = await api.get('/v1/goals');
      setGoals(data);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch goals');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchGoals();
  }, []);

  const handleInputChange = (e) => {
    const { name, value } = e.target;
    setNewGoal((prev) => ({ ...prev, [name]: value }));
  };

  const handleCreate = async (e) => {
    e.preventDefault();
    try {
      await api.post('/v1/goals', newGoal);
      setNewGoal({ title: '', description: '', status: 'active', parent_id: '' });
      fetchGoals();
    } catch (err) {
      setError(err.message || 'Failed to create goal');
    }
  };

  if (loading) return <div>Loading goals...</div>;
  if (error) return <div className="error">{error}</div>;

  return (
    <div className="goals-tab">
      <h2>Society Goal Map</h2>
      <div className="goal-list">
        {goals.map((goal) => (
          <div key={goal.id} className="card">
            <h3>{goal.title}</h3>
            <p>{goal.description}</p>
            <span className="status-pill">{goal.status}</span>
            {goal.parent_id && <p>Parent: {goal.parent_id}</p>}
          </div>
        ))}
      </div>
      <div className="create-goal-form">
        <h3>Create New Goal</h3>
        <form onSubmit={handleCreate}>
          <input
            type="text"
            name="title"
            placeholder="Goal title"
            value={newGoal.title}
            onChange={handleInputChange}
            required
          />
          <textarea
            name="description"
            placeholder="Description"
            value={newGoal.description}
            onChange={handleInputChange}
            required
          />
          <select name="status" value={newGoal.status} onChange={handleInputChange}>
            <option value="active">Active</option>
            <option value="completed">Completed</option>
            <option value="abandoned">Abandoned</option>
          </select>
          <input
            type="text"
            name="parent_id"
            placeholder="Parent goal ID (optional)"
            value={newGoal.parent_id}
            onChange={handleInputChange}
          />
          <button type="submit">Create Goal</button>
        </form>
      </div>
    </div>
  );
};

export default Goals;