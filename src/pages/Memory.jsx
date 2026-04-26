import React, { useState, useEffect } from 'react';
import { getMemory } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import Card from '../components/Card';
import StatusPill from '../components/StatusPill';
import { toast } from 'react-hot-toast';

const TAGS = ['society', 'agent', 'lesson'];

const Memory = () => {
  const { token } = useAuth();
  const [memoryItems, setMemoryItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [activeTag, setActiveTag] = useState('all');

  useEffect(() => {
    if (token) {
      fetchMemory();
    }
  }, [token]);

  const fetchMemory = async (tag) => {
    setLoading(true);
    try {
      const data = await getMemory(token, tag && tag !== 'all' ? { tag } : {});
      setMemoryItems(data);
    } catch (err) {
      toast.error('Failed to load memory');
    } finally {
      setLoading(false);
    }
  };

  const handleTagClick = (tag) => {
    setActiveTag(tag);
    fetchMemory(tag);
  };

  if (loading) return <div className="text-center p-8"><span className="loading loading-spinner loading-lg"></span></div>;

  return (
    <div className="container mx-auto p-4 space-y-4">
      <h1 className="text-2xl font-bold">Memory</h1>

      {/* Tag filter */}
      <div className="tabs">
        <button className={`tab tab-bordered ${activeTag === 'all' ? 'tab-active' : ''}`} onClick={() => handleTagClick('all')}>All</button>
        {TAGS.map((tag) => (
          <button key={tag} className={`tab tab-bordered ${activeTag === tag ? 'tab-active' : ''}`} onClick={() => handleTagClick(tag)}>
            {tag.charAt(0).toUpperCase() + tag.slice(1)}
          </button>
        ))}
      </div>

      {/* Memory items */}
      <div className="space-y-2">
        {memoryItems.length === 0 && <p className="text-gray-500">No memory items.</p>}
        {memoryItems.map((item) => (
          <Card key={item.id} title={item.type}>
            <p>{item.content}</p>
            <div className="mt-2 flex gap-2 flex-wrap">
              {item.tags && item.tags.map((tag) => (
                <StatusPill key={tag} status={tag} />
              ))}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
};

export default Memory;