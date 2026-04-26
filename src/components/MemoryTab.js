import React, { useState, useEffect } from 'react';
import apiClient from '../api/client';
import StatusPill from './StatusPill';

const MemoryTab = () => {
  const [lessons, setLessons] = useState([]);
  const [loading, setLoading] = useState(true);
  const [tagFilter, setTagFilter] = useState('');
  const [allTags, setAllTags] = useState([]);

  useEffect(() => {
    fetchLessons();
  }, [tagFilter]);

  const fetchLessons = async () => {
    try {
      const params = {};
      if (tagFilter) params.tag = tagFilter;
      const res = await apiClient.get('/v1/memory', { params });
      setLessons(res.data);
      // Extract unique tags for filter
      const tags = [...new Set(res.data.flatMap((l) => l.tags || []))];
      setAllTags(tags);
    } catch (err) {
      console.error('Failed to load memory', err);
    } finally {
      setLoading(false);
    }
  };

  if (loading) return <div className="loading-spinner">Loading memory...</div>;

  return (
    <div className="memory-tab">
      <h2>Memory: Lessons Learned</h2>
      <div className="memory-filter">
        <label>Filter by tag:</label>
        <select value={tagFilter} onChange={(e) => setTagFilter(e.target.value)}>
          <option value="">All tags</option>
          {allTags.map((tag) => (
            <option key={tag} value={tag}>{tag}</option>
          ))}
        </select>
      </div>
      <div className="lessons-list">
        {lessons.length === 0 && <p>No lessons found.</p>}
        {lessons.map((lesson) => (
          <div key={lesson.id} className="card">
            <div className="card-header">
              <h3>{lesson.source_type}: {lesson.source_name}</h3>
              <StatusPill status={lesson.type} />
            </div>
            <p className="card-description">{lesson.content}</p>
            <div className="card-tags">
              {lesson.tags?.map((tag) => (
                <span key={tag} className="tag">{tag}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default MemoryTab;