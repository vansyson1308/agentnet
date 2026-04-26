import React, { useEffect, useState } from 'react';
import api from '../utils/api';

function OffersPage() {
  const [offers, setOffers] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchOffers();
  }, []);

  const fetchOffers = async () => {
    try {
      const response = await api.get('/v1/offers');
      setOffers(response);
    } catch (err) {
      console.error('Failed to fetch offers:', err);
    } finally {
      setLoading(false);
    }
  };

  if (loading) return <div className="loading">Loading offers...</div>;

  return (
    <div className="page offers-page">
      <h1>Offers</h1>
      {offers.length === 0 ? (
        <p>No offers available.</p>
      ) : (
        <div className="offer-list">
          {offers.map((offer) => (
            <div key={offer.id} className="card">
              <h3>{offer.title}</h3>
              <p>{offer.description}</p>
              <span className="status-pill">{offer.status}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default OffersPage;