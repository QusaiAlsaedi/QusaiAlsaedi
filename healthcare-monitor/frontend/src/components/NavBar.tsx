import React from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { clearToken } from '../api/client';

interface NavBarProps {
  alertCount?: { critical: number; warning: number };
}

const NavBar: React.FC<NavBarProps> = ({ alertCount }) => {
  const location = useLocation();
  const navigate = useNavigate();

  const isActive = (path: string) =>
    location.pathname === path ? styles.activeLink : styles.link;

  const handleLogout = () => {
    clearToken();
    navigate('/login');
  };

  const critCount = alertCount?.critical || 0;
  const warnCount = alertCount?.warning || 0;

  return (
    <nav style={styles.nav}>
      <div style={styles.brand}>
        <span style={styles.brandIcon}>[+]</span>
        <span style={styles.brandText}>INTEGRATION MONITOR</span>
      </div>
      <div style={styles.links}>
        <Link to="/" style={isActive('/')}>OVERVIEW</Link>
        <Link to="/stuck" style={isActive('/stuck')}>STUCK MESSAGES</Link>
        <Link to="/endpoints" style={isActive('/endpoints')}>ENDPOINTS</Link>
      </div>
      <div style={styles.right}>
        {critCount > 0 && (
          <span style={styles.critBadge}>[!] {critCount} CRITICAL</span>
        )}
        {warnCount > 0 && (
          <span style={styles.warnBadge}>[!] {warnCount} WARN</span>
        )}
        <button onClick={handleLogout} style={styles.logoutBtn}>LOGOUT</button>
      </div>
    </nav>
  );
};

const styles: Record<string, React.CSSProperties> = {
  nav: {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    background: '#0a0a0a', borderBottom: '2px solid #333',
    padding: '10px 20px', fontFamily: 'monospace',
  },
  brand: { display: 'flex', alignItems: 'center', gap: 8 },
  brandIcon: { color: '#00ff88', fontSize: 18, fontWeight: 'bold' },
  brandText: { color: '#00ff88', fontSize: 14, fontWeight: 'bold', letterSpacing: 2 },
  links: { display: 'flex', gap: 24 },
  link: { color: '#aaa', textDecoration: 'none', fontSize: 13, letterSpacing: 1 },
  activeLink: { color: '#00ff88', textDecoration: 'none', fontSize: 13,
                letterSpacing: 1, borderBottom: '2px solid #00ff88', paddingBottom: 2 },
  right: { display: 'flex', alignItems: 'center', gap: 12 },
  critBadge: { background: '#ff2020', color: '#fff', padding: '3px 8px',
               fontSize: 12, fontFamily: 'monospace', borderRadius: 2 },
  warnBadge: { background: '#ff8800', color: '#fff', padding: '3px 8px',
               fontSize: 12, fontFamily: 'monospace', borderRadius: 2 },
  logoutBtn: {
    background: 'none', border: '1px solid #555', color: '#888',
    padding: '4px 10px', fontSize: 12, cursor: 'pointer', fontFamily: 'monospace',
  },
};

export default NavBar;
