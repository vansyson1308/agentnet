import React, { useState, useEffect } from 'react';
import { getMemory } from '../api';

function MemoryTab() {
  const [memoryItems, setMemoryItems] = useState([]);
  const [tagFilter, setTagFilter] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    fetchMemory();
  }, [tagFilter]);

  async function fetchMemory() {
    try {
      const data = await getMemory(tagFilter);
      setMemoryItems(data);
    } catch (err) {
      setError('Failed to load memory');
    }
  }

  return (
    <div className="tab-content">
      <h2>Memory (Lessons)</h2>
      <div className="filter-bar">
        <input
          type="text"
          placeholder="Filter by tag"
          value={tagFilter}
          onChange={e => setTagFilter(e.target.value)}
        />
      </div>
      {error && <div className="error">{error}</div>}
      <div className="memory-list">
        {memoryItems.length === 0 ? (
          <p>No memory items found.</p>
        ) : (
          memoryItems.map(item => (
            <div key={item.id} className="card">
              <p><strong>Society:</strong> {item.society_lesson}</p>
              {item.agent_lesson && <p><strong>Agent:</strong> {item.agent_lesson}</p>}
              {item.tags && item.tags.length > 0 && (
                <div className="tags">
                  {item.tags.map((tag, idx) => (
                    <span key={idx} className="tag-pill">{tag}</span>
                  ))}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export default MemoryTab;