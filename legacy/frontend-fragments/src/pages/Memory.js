import React, { useState, useEffect } from 'react';
import { api } from '../api';
import { Cards, StatusPill } from '../components';

const Memory = () => {
  const [lessons, setLessons] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tagFilter, setTagFilter] = useState('');

  useEffect(() => {
    fetchLessons();
  }, [tagFilter]);

  const fetchLessons = async () => {
    try {
      setLoading(true);
      const params = {};
      if (tagFilter.trim()) params.tag = tagFilter.trim();
      const response = await api.get('/v1/memory', { params });
      setLessons(response.data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  if (loading) return <div className="loading">Loading memory...</div>;
  if (error) return <div className="error">Error: {error}</div>;

  return (
    <div className="memory-page">
      <h1>Memory & Lessons</h1>
      <div className="filter-bar">
        <input
          type="text"
          placeholder="Filter by tag..."
          value={tagFilter}
          onChange={(e) => setTagFilter(e.target.value)}
        />
      </div>
      <div className="lessons-section">
        <h2>Society Lessons</h2>
        {lessons.filter(l => l.scope === 'society').length === 0 ? (
          <p>No society lessons.</p>
        ) : (
          lessons.filter(l => l.scope === 'society').map(lesson => (
            <div key={lesson.id} className="lesson-card">
              <Cards>
                <h3>{lesson.title}</h3>
                <p>{lesson.content}</p>
                {lesson.tags && lesson.tags.map(tag => <StatusPill key={tag} label={tag} />)}
              </Cards>
            </div>
          ))
        )}
      </div>
      <div className="lessons-section">
        <h2>Agent Lessons</h2>
        {lessons.filter(l => l.scope === 'agent').length === 0 ? (
          <p>No agent lessons.</p>
        ) : (
          lessons.filter(l => l.scope === 'agent').map(lesson => (
            <div key={lesson.id} className="lesson-card">
              <Cards>
                <h3>{lesson.title}</h3>
                <p>{lesson.content}</p>
                {lesson.tags && lesson.tags.map(tag => <StatusPill key={tag} label={tag} />)}
              </Cards>
            </div>
          ))
        )}
      </div>
    </div>
  );
};

export default Memory;