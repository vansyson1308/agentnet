import React from 'react';
import { Link, useLocation } from 'react-router-dom';

function NavBar() {
  const location = useLocation();
  const links = [
    { to: '/', label: 'Dashboard' },
    { to: '/agents', label: 'Agents' },
    { to: '/offers', label: 'Offers' },
    { to: '/goals', label: 'Goals' },
    { to: '/lab', label: 'Lab' },
    { to: '/memory', label: 'Memory' },
  ];

  return (
    <nav className="navbar">
      <div className="navbar-brand">AgentNet</div>
      <div className="navbar-links">
        {links.map((link) => (
          <Link
            key={link.to}
            to={link.to}
            className={`nav-link ${location.pathname === link.to ? 'active' : ''}`}
          >
            {link.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}

export default NavBar;