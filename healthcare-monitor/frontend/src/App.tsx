import React, { useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import NavBar from './components/NavBar';
import Overview from './pages/Overview';
import StuckMessages from './pages/StuckMessages';
import EndpointStatus from './pages/EndpointStatus';
import Login from './pages/Login';
import { alertsApi } from './api/client';

function PrivateRoute({ children }: { children: React.ReactNode }) {
  const token = localStorage.getItem('token');
  return token ? <>{children}</> : <Navigate to="/login" replace />;
}

const App: React.FC = () => {
  const [alertCount, setAlertCount] = useState({ critical: 0, warning: 0 });

  useEffect(() => {
    const token = localStorage.getItem('token');
    if (!token) return;

    const fetchAlertCount = async () => {
      try {
        const res = await alertsApi.activeCount();
        setAlertCount({ critical: res.data.critical || 0, warning: res.data.warning || 0 });
      } catch { /* ignore */ }
    };

    fetchAlertCount();
    const timer = setInterval(fetchAlertCount, 30_000);
    return () => clearInterval(timer);
  }, []);

  const isLoggedIn = !!localStorage.getItem('token');

  return (
    <BrowserRouter>
      {isLoggedIn && <NavBar alertCount={alertCount} />}
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<PrivateRoute><Overview /></PrivateRoute>} />
        <Route path="/stuck" element={<PrivateRoute><StuckMessages /></PrivateRoute>} />
        <Route path="/endpoints" element={<PrivateRoute><EndpointStatus /></PrivateRoute>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
};

export default App;
