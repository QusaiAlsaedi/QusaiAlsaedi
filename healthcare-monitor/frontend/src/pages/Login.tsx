import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { authApi, setToken } from '../api/client';

const Login: React.FC = () => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      const res = await authApi.login(username, password);
      setToken(res.data.access_token);
      navigate('/');
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Login failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={styles.page}>
      <div style={styles.box}>
        <div style={styles.title}>[+] INTEGRATION MONITOR</div>
        <div style={styles.sub}>Hospital RIS/HIS Integration Health Platform</div>
        <form onSubmit={handleSubmit} style={styles.form}>
          <div style={styles.field}>
            <label style={styles.label}>USERNAME</label>
            <input
              style={styles.input} type="text" value={username} autoFocus
              onChange={e => setUsername(e.target.value)} required
            />
          </div>
          <div style={styles.field}>
            <label style={styles.label}>PASSWORD</label>
            <input
              style={styles.input} type="password" value={password}
              onChange={e => setPassword(e.target.value)} required
            />
          </div>
          {error && <div style={styles.error}>{error}</div>}
          <button style={styles.btn} type="submit" disabled={loading}>
            {loading ? 'AUTHENTICATING...' : 'LOGIN'}
          </button>
        </form>
        <div style={styles.footer}>Default: admin / ChangeMe123! (change immediately)</div>
      </div>
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  page: {
    background: '#0a0a0a', minHeight: '100vh',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontFamily: 'monospace',
  },
  box: {
    background: '#111', border: '1px solid #222', padding: 40,
    width: 360, textAlign: 'center',
  },
  title: { color: '#00ff88', fontSize: 18, fontWeight: 'bold', letterSpacing: 2, marginBottom: 8 },
  sub: { color: '#555', fontSize: 11, marginBottom: 32, letterSpacing: 1 },
  form: { display: 'flex', flexDirection: 'column', gap: 16 },
  field: { textAlign: 'left' },
  label: { display: 'block', color: '#555', fontSize: 11, letterSpacing: 2, marginBottom: 6 },
  input: {
    width: '100%', boxSizing: 'border-box',
    background: '#0d0d0d', border: '1px solid #333', color: '#aaa',
    padding: '10px 12px', fontSize: 14, fontFamily: 'monospace',
  } as React.CSSProperties,
  error: { background: '#1a0000', color: '#ff4444', padding: '8px 12px', fontSize: 12 },
  btn: {
    background: '#00ff88', border: 'none', color: '#000',
    padding: '12px', fontSize: 13, cursor: 'pointer',
    fontFamily: 'monospace', fontWeight: 'bold', letterSpacing: 2,
    marginTop: 8,
  },
  footer: { color: '#333', fontSize: 10, marginTop: 24 },
};

export default Login;
