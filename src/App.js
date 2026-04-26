import React from 'react';
import { BrowserRouter as Router, Routes, Route, NavLink } from 'react-router-dom';
import DashboardTab from './components/DashboardTab';
import AgentsTab from './components/AgentsTab';
import OffersTab from './components/OffersTab';
import GoalsTab from './components/GoalsTab';
import LabTab from './components/LabTab';
import MemoryTab from './components/MemoryTab';
import AgentProfilePage from './components/AgentProfilePage';
import './App.css';

function App() {
  return (
    <Router>
      <div className="App">
        <nav className="top-nav">
          <NavLink to="/" end>Dashboard</NavLink>
          <NavLink to="/agents">Agents</NavLink>
          <NavLink to="/offers">Offers</NavLink>
          <NavLink to="/goals">Goals</NavLink>
          <NavLink to="/lab">Lab</NavLink>
          <NavLink to="/memory">Memory</NavLink>
        </nav>
        <main className="content">
          <Routes>
            <Route path="/" element={<DashboardTab />} />
            <Route path="/agents" element={<AgentsTab />} />
            <Route path="/agents/:id" element={<AgentProfilePage />} />
            <Route path="/offers" element={<OffersTab />} />
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