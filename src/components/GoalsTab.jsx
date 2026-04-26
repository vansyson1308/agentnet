import React, { useState, useEffect } from 'react';
import { getGoals, createGoal } from '../api';

function GoalsTab() {
  const [goals, setGoals] = useState([]);
  const [newGoalTitle, setNewGoalTitle] = useState('');
  const [newGoalDescription, setNewGoalDescription] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    fetchGoals();
  }, []);

  async function fetchGoals() {
    try {
      const data = await getGoals();
      setGoals(data);
    } catch (err) {
      setError('Failed to load goals');
    }
  }

  async function handleCreateGoal(e) {
    e.preventDefault();
    try {
      await createGoal({ title: newGoalTitle, description: newGoalDescription });
      setNewGoalTitle('');
      setNewGoalDescription('');
      fetchGoals();
    } catch (err) {
      setError('Failed to create goal');
    }
  }

  return (
    <div className="tab-content">
      <h2>Society Goal Map</h2>
      {error && <div className="error">{error}</div>}
      <div className="goal-map">
        {goals.length === 0 ? (
          <p>No goals defined yet.</p>
        ) : (
          goals.map(goal => (
            <div key={goal.id} className="card">
              <h3>{goal.title}</h3>
              <p>{goal.description}</p>
              <span className="status-pill">Active: {goal.is_active ? 'Yes' : 'No'}</span>
            </div>
          ))
        )}
      </div>
      <h3>Create New Goal</h3>
      <form onSubmit={handleCreateGoal} className="create-form">
        <input
          type="text"
          placeholder="Goal title"
          value={newGoalTitle}
          onChange={e => setNewGoalTitle(e.target.value)}
          required
        />
        <textarea
          placeholder="Description"
          value={newGoalDescription}
          onChange={e => setNewGoalDescription(e.target.value)}
        />
        <button type="submit">Create Goal</button>
      </form>
    </div>
  );
}

export default GoalsTab;