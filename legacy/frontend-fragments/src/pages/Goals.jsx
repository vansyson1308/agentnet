import React, { useState, useEffect, useCallback } from 'react';
import { getGoals, createGoal } from '../api/client';
import Card from '../components/Card';
import StatusPill from '../components/StatusPill';
import { useAuth } from '../hooks/useAuth';

const initialForm = { name: '', description: '', kpi: '', weight: 0, parentId: null };

export default function Goals() {
  const { user } = useAuth();
  const [goals, setGoals] = useState([]);
  const [form, setForm] = useState(initialForm);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const fetchGoals = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getGoals();
      setGoals(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchGoals();
  }, [fetchGoals]);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setForm((prev) => ({ ...prev, [name]: value }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!form.name.trim() || !form.description.trim()) return;
    try {
      setSubmitting(true);
      await createGoal(form);
      setForm(initialForm);
      await fetchGoals();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return <div>Loading goals...</div>;
  if (error) return <div>Error: {error}</div>;

  return (
    <div className="goals-page">
      <h2>Society Goal Map</h2>
      <div className="goals-list">
        {goals.map((goal) => (
          <Card key={goal.id} title={goal.name}>
            <p>{goal.description}</p>
            {goal.kpi && <p>KPI: {goal.kpi} (weight: {goal.weight})</p>}
            <StatusPill status={goal.active ? 'active' : 'inactive'} />
            {goal.parentId && <small>Parent goal ID: {goal.parentId}</small>}
          </Card>
        ))}
      </div>

      {user?.role === 'admin' && (
        <form className="goal-create-form" onSubmit={handleSubmit}>
          <h3>Create New Goal</h3>
          <label>
            Name:
            <input name="name" value={form.name} onChange={handleChange} required />
          </label>
          <label>
            Description:
            <textarea name="description" value={form.description} onChange={handleChange} required />
          </label>
          <label>
            KPI (optional):
            <input name="kpi" value={form.kpi} onChange={handleChange} />
          </label>
          <label>
            Weight:
            <input name="weight" type="number" value={form.weight} onChange={handleChange} />
          </label>
          <label>
            Parent Goal ID (optional):
            <input name="parentId" type="number" value={form.parentId || ''} onChange={handleChange} />
          </label>
          <button type="submit" disabled={submitting}>
            {submitting ? 'Creating...' : 'Create Goal'}
          </button>
        </form>
      )}
    </div>
  );
}