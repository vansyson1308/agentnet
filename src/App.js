import React from 'react';
import { BrowserRouter as Router, Routes, Route, Link } from 'react-router-dom';
import Dashboard from './components/Dashboard';
import AgentsList from './components/AgentsList';
import OffersList from './components/OffersList';
import AgentProfile from './components/AgentProfile';
import GoalsTab from './components/GoalsTab';
import LabTab from './components/LabTab';
import MemoryTab from './components/MemoryTab';

function App() {
  return (
    <Router>
      <div>
        <nav className="top-nav">
          <ul>
            <li><Link to="/">Dashboard</Link></li>
            <li><Link to="/agents">Agents</Link></li>
            <li><Link to="/offers">Offers</Link></li>
            <li><Link to="/goals">Goals</Link></li>
            <li><Link to="/lab">Lab</Link></li>
            <li><Link to="/memory">Memory</Link></li>
          </ul>
        </nav>
        <main className="container">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/agents" element={<AgentsList />} />
            <Route path="/agents/:id" element={<AgentProfile />} />
            <Route path="/offers" element={<OffersList />} />
            <Route path="/goals" element={<GoalsTab />} />
            <Route path="/lab" element={<LabTab />} />
            <Route path="/memory" element={<MemoryTab />} />
          </Routes>
        </main>
      </div>
    </Router>
  );
}

export default App;