import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';
import Logo from './Logo';

const NAV_LINKS = [
  { path: '/dashboard', label: 'Dashboard' },
  { path: '/agents', label: 'Agents' },
  { path: '/offers', label: 'Offers' },
  { path: '/goals', label: 'Goals' },
  { path: '/lab', label: 'Lab' },
  { path: '/memory', label: 'Memory' },
];

const Navbar = () => {
  const { token, logout } = useAuth();
  const location = useLocation();

  return (
    <div className="navbar bg-base-100 shadow-lg">
      <div className="flex-1">
        <Logo />
        <ul className="menu menu-horizontal px-1">
          {token && NAV_LINKS.map((link) => (
            <li key={link.path}>
              <Link
                to={link.path}
                className={location.pathname.startsWith(link.path) ? 'active' : ''}
              >
                {link.label}
              </Link>
            </li>
          ))}
        </ul>
      </div>
      <div className="flex-none">
        {token && (
          <button className="btn btn-ghost" onClick={logout}>Logout</button>
        )}
      </div>
    </div>
  );
};

export default Navbar;