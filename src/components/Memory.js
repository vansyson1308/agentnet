import React, { useState, useEffect } from 'react';
import api from '../api/client';

const Memory = () => {
  const [memories, setMemories] = useState([]);
  const [tagFilter, setTagFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchMemories = async () => {
    try {
      setLoading(true);
      const params = tagFilter ? { tag: tagFilter } : {};
      const data = await api.get('/v1/memory', params);
      setMemories(data);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch memories');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMemories();
  }, [tagFilter]);

  const handleTagChange = (e) => {
    setTagFilter(e.target.value);
  };

  if (loading) return <div>Loading memories...</div>;
  if (error) return <div className="error">{error}</div>;

  return (
    <div className="memory-tab">
      <h2>Memory</h2>
      <div className="tag-filter">
        <label>Filter by tag: </label>
        <input
          type="text"
          value={tagFilter}
          onChange={handleTagChange}
          placeholder="e.g. society, agent"
        />
      </div>
      <div className="memory-list">
        {memories.map((memory) => (
          <div key={memory.id} className="card">
            <p>{memory.content}</p>
            <div className="tags">
              {(memory.tags || []).map((tag) => (
                <span key={tag} className="tag">{tag}</span>
              ))}
            </div>
            <small>{memory.type} - {memory.source}</small>
          </div>
        ))}
      </div>
    </div>
  );
};

export default Memory;