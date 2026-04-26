import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import NavBar from './components/NavBar';
import DashboardPage from './pages/DashboardPage';
import AgentsPage from './pages/AgentsPage';
import OffersPage from './pages/OffersPage';
import GoalsPage from './pages/GoalsPage';
import LabPage from './pages/LabPage';
import MemoryPage from './pages/MemoryPage';
import AgentProfilePage from './pages/AgentProfilePage';
import './App.css';

function App() {
  return (
    <Router>
      <div className="App">
        <NavBar />
        <main className="content">
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/agents/:id" element={<AgentProfilePage />} />
            <Route path="/offers" element={<OffersPage />} />
            <Route path="/goals" element={<GoalsPage />} />
            <Route path="/lab" element={<LabPage />} />
            <Route path="/memory" element={<MemoryPage />} />
          </Routes>
        </main>
      </div>
    </Router>
  );
}

export default App;