import React from 'react';
import { BrowserRouter as Router, Routes, Route, Link } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import Agents from './pages/Agents';
import AgentProfile from './pages/AgentProfile';
import Offers from './pages/Offers';
import Goals from './components/Goals';
import Lab from './components/Lab';
import Memory from './components/Memory';
import './App.css';

function App() {
  return (
    <Router>
      <div className="App">
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
        <main>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/agents/:id" element={<AgentProfile />} />
            <Route path="/offers" element={<Offers />} />
            <Route path="/goals" element={<Goals />} />
            <Route path="/lab" element={<Lab />} />
            <Route path="/memory" element={<Memory />} />
          </Routes>
        </main>
      </div>
    </Router>
  );
}

export default App;