import React, { useEffect, useState } from 'react';
import api from '../utils/api';

function MemoryPage() {
  const [memoryItems, setMemoryItems] = useState([]);
  const [selectedTag, setSelectedTag] = useState('all');
  const [tags, setTags] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchMemory();
  }, [selectedTag]);

  const fetchMemory = async () => {
    try {
      const params = selectedTag === 'all' ? {} : { tag: selectedTag };
      const response = await api.get('/v1/memory', { params });
      setMemoryItems(response.data);
      // Extract unique tags
      const allTags = [...new Set(response.data.flatMap(item => item.tags))];
      setTags(allTags);
    } catch (err) {
      console.error('Failed to fetch memory:', err);
    } finally {
      setLoading(false);
    }
  };

  if (loading) return <div className="loading">Loading memory...</div>;

  return (
    <div className="page memory-page">
      <h1>Society Memory</h1>
      <div className="tag-filter">
        <label>Filter by tag: </label>
        <select value={selectedTag} onChange={(e) => setSelectedTag(e.target.value)}>
          <option value="all">All</option>
          {tags.map((tag) => (
            <option key={tag} value={tag}>{tag}</option>
          ))}
        </select>
      </div>
      <div className="memory-list">
        {memoryItems.length === 0 ? (
          <p>No memory items found.</p>
        ) : (
          <div className="memory-grid">
            {memoryItems.map((item) => (
              <div key={item.id} className="card">
                <div className="card-header">
                  <span className="memory-type">{item.type}</span>
                  <span className="memory-source">{item.source}</span>
                </div>
                <div className="card-body">
                  <p>{item.content}</p>
                </div>
                <div className="card-footer">
                  {item.tags.map((tag) => (
                    <span key={tag} className="tag-pill">{tag}</span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default MemoryPage;