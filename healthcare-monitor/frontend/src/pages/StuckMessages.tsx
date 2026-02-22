/**
 * Stuck Messages page - shows all outbound HL7 messages with no ACK.
 * Sortable table with age highlighting and bulk actions.
 */
import React, { useEffect, useState, useCallback } from 'react';
import { hl7Api } from '../api/client';
import { StuckMessage } from '../types';

const REFRESH_MS = 15_000;

type SortKey = 'age_seconds' | 'message_type' | 'receiving_application' | 'server_name';

const StuckMessages: React.FC = () => {
  const [messages, setMessages] = useState<StuckMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [minAge, setMinAge] = useState(30);
  const [sortKey, setSortKey] = useState<SortKey>('age_seconds');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [filterType, setFilterType] = useState('');
  const [lastRefresh, setLastRefresh] = useState(new Date());

  const refresh = useCallback(async () => {
    try {
      const res = await hl7Api.stuck(minAge);
      setMessages(res.data);
      setLastRefresh(new Date());
      setError(null);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, [minAge]);

  useEffect(() => {
    setLoading(true);
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  };

  const sorted = [...messages]
    .filter(m => !filterType || m.message_type === filterType)
    .sort((a, b) => {
      const av = a[sortKey] ?? '';
      const bv = b[sortKey] ?? '';
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sortDir === 'asc' ? cmp : -cmp;
    });

  const msgTypes = Array.from(new Set(messages.map(m => m.message_type)));

  const colHeader = (label: string, key: SortKey) => (
    <th style={styles.th} onClick={() => toggleSort(key)}>
      {label} {sortKey === key ? (sortDir === 'desc' ? ' v' : ' ^') : ''}
    </th>
  );

  return (
    <div style={styles.page}>
      <div style={styles.header}>
        <span style={styles.title}>STUCK MESSAGES</span>
        <span style={styles.count}>
          {sorted.length} message(s) with no ACK
          {sorted.length > 0 && (
            <span style={{ color: '#ff2020', marginLeft: 8 }}>[ACTION REQUIRED]</span>
          )}
        </span>
      </div>

      <div style={styles.toolbar}>
        <label style={styles.label}>Min age (s):
          <input
            type="number" value={minAge} min={5} step={15}
            onChange={e => setMinAge(Number(e.target.value))}
            style={styles.input}
          />
        </label>
        <label style={styles.label}>Type:
          <select value={filterType} onChange={e => setFilterType(e.target.value)}
                  style={styles.select}>
            <option value="">ALL</option>
            {msgTypes.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <span style={styles.refreshTime}>Refreshed: {lastRefresh.toLocaleTimeString()}</span>
        <button style={styles.btn} onClick={refresh}>REFRESH</button>
      </div>

      {error && <div style={styles.error}>[ERROR] {error}</div>}

      {loading ? (
        <div style={styles.loading}>Loading...</div>
      ) : sorted.length === 0 ? (
        <div style={styles.clear}>
          [OK] No stuck messages found (min age: {minAge}s). Integration flow is healthy.
        </div>
      ) : (
        <div style={styles.tableWrap}>
          <table style={styles.table}>
            <thead>
              <tr>
                {colHeader('AGE', 'age_seconds')}
                {colHeader('TYPE', 'message_type')}
                <th style={styles.th}>CTRL ID</th>
                <th style={styles.th}>FROM</th>
                {colHeader('TO', 'receiving_application')}
                {colHeader('SERVER', 'server_name')}
                <th style={styles.th}>FLOW</th>
                <th style={styles.th}>DETECTED</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((m) => (
                <tr key={m.id} style={rowStyle(m.age_seconds)}>
                  <td style={{ ...styles.td, ...ageStyle(m.age_seconds) }}>
                    {m.age_human}
                  </td>
                  <td style={styles.td}>
                    <span style={typeStyle(m.message_type)}>{m.message_type}</span>
                  </td>
                  <td style={styles.td}>
                    <code style={styles.code}>{m.message_control_id}</code>
                  </td>
                  <td style={styles.td}>{m.sending_application || '--'}</td>
                  <td style={styles.td}>{m.receiving_application || '--'}</td>
                  <td style={styles.td}>{m.server_name || '--'}</td>
                  <td style={styles.td}>{m.flow_name || '--'}</td>
                  <td style={styles.td}>{_fmtDt(m.detected_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {sorted.length > 0 && (
        <div style={styles.summary}>
          SUMMARY: {sorted.length} stuck |{' '}
          Oldest: {sorted[0]?.age_human} |{' '}
          Types: {msgTypes.join(', ')} |{' '}
          Servers: {Array.from(new Set(sorted.map(m => m.server_name).filter(Boolean))).join(', ')}
        </div>
      )}
    </div>
  );
};

function rowStyle(ageSeconds: number): React.CSSProperties {
  if (ageSeconds > 300) return { background: '#1a0000' };
  if (ageSeconds > 60) return { background: '#1a0e00' };
  return {};
}

function ageStyle(ageSeconds: number): React.CSSProperties {
  if (ageSeconds > 300) return { color: '#ff2020', fontWeight: 'bold' };
  if (ageSeconds > 60) return { color: '#ff8800' };
  return { color: '#ffdd44' };
}

function typeStyle(type: string): React.CSSProperties {
  const colors: Record<string, string> = {
    ORM: '#ff8844', ORU: '#44aaff', ORR: '#88ff44', ADT: '#ff44aa',
  };
  return { color: colors[type] || '#aaa', fontWeight: 'bold' };
}

function _fmtDt(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString();
  } catch {
    return iso;
  }
}

const styles: Record<string, React.CSSProperties> = {
  page: { background: '#0d0d0d', minHeight: '100vh', padding: 20, fontFamily: 'monospace' },
  header: { display: 'flex', alignItems: 'center', gap: 20, marginBottom: 20 },
  title: { color: '#ff8800', fontSize: 18, fontWeight: 'bold', letterSpacing: 2 },
  count: { color: '#888', fontSize: 13 },
  toolbar: {
    display: 'flex', alignItems: 'center', gap: 20, marginBottom: 16,
    padding: '10px 14px', background: '#111', border: '1px solid #1a1a1a',
  },
  label: { color: '#666', fontSize: 12, display: 'flex', alignItems: 'center', gap: 8 },
  input: {
    background: '#0d0d0d', border: '1px solid #333', color: '#aaa',
    padding: '4px 8px', fontSize: 12, width: 60, fontFamily: 'monospace',
  },
  select: {
    background: '#0d0d0d', border: '1px solid #333', color: '#aaa',
    padding: '4px 8px', fontSize: 12, fontFamily: 'monospace',
  },
  refreshTime: { color: '#555', fontSize: 11, flex: 1 },
  btn: {
    background: 'none', border: '1px solid #333', color: '#666',
    padding: '4px 12px', fontSize: 12, cursor: 'pointer', fontFamily: 'monospace',
  },
  error: { background: '#1a0000', color: '#ff6666', padding: 10, marginBottom: 12,
            fontSize: 12, borderLeft: '4px solid #ff2020' },
  loading: { color: '#555', padding: 20 },
  clear: { color: '#00ff88', padding: 20, border: '1px solid #1a2a1a',
            background: '#0a1a0a', fontSize: 13 },
  tableWrap: { overflowX: 'auto' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
  th: {
    color: '#555', padding: '8px 12px', textAlign: 'left', cursor: 'pointer',
    borderBottom: '1px solid #1a1a1a', letterSpacing: 1, userSelect: 'none',
    whiteSpace: 'nowrap',
  } as React.CSSProperties,
  td: { color: '#aaa', padding: '8px 12px', borderBottom: '1px solid #111', whiteSpace: 'nowrap' },
  code: { color: '#888', fontSize: 11 },
  summary: {
    marginTop: 16, color: '#666', fontSize: 11, letterSpacing: 1,
    padding: '8px 12px', background: '#111', borderTop: '1px solid #1a1a1a',
  },
};

export default StuckMessages;
