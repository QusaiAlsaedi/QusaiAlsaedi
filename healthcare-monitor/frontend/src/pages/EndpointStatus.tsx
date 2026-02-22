/**
 * Endpoint Status page - shows all monitored network endpoints,
 * current reachability, latency, and probe history.
 */
import React, { useEffect, useState, useCallback } from 'react';
import { endpointsApi, probesApi } from '../api/client';
import { Endpoint, ProbeOut } from '../types';

const REFRESH_MS = 30_000;

const STATUS_COLOR: Record<string, string> = {
  up: '#00ff88', down: '#ff2020', timeout: '#ff8800', error: '#ff8800', unknown: '#666',
};

const EndpointStatus: React.FC = () => {
  const [endpoints, setEndpoints] = useState<Endpoint[]>([]);
  const [latestProbes, setLatestProbes] = useState<ProbeOut[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState(new Date());
  const [showAddForm, setShowAddForm] = useState(false);
  const [newEndpoint, setNewEndpoint] = useState({
    name: '', host: '', port: 6661, protocol: 'tcp',
    check_interval_seconds: 30, timeout_seconds: 5, tags: [] as string[], metadata: {},
  });

  const refresh = useCallback(async () => {
    try {
      const [epRes, probeRes] = await Promise.all([
        endpointsApi.list(),
        probesApi.latest(200),
      ]);
      setEndpoints(epRes.data);
      setLatestProbes(probeRes.data);
      setLastRefresh(new Date());
      setError(null);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const loadHistory = async (epId: string) => {
    const res = await endpointsApi.probes(epId, 4);
    setHistory(res.data);
    setSelectedId(epId);
  };

  const handleAdd = async () => {
    try {
      await endpointsApi.create(newEndpoint);
      setShowAddForm(false);
      setNewEndpoint({
        name: '', host: '', port: 6661, protocol: 'tcp',
        check_interval_seconds: 30, timeout_seconds: 5, tags: [], metadata: {},
      });
      await refresh();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e.message);
    }
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm('Remove this endpoint?')) return;
    await endpointsApi.delete(id);
    await refresh();
  };

  // Compute uptime % from latest probes per endpoint
  const uptimeMap: Record<string, string> = {};
  latestProbes.forEach(p => {
    // Just color-code by latest status for simplicity
    if (!uptimeMap[p.endpoint_id]) {
      uptimeMap[p.endpoint_id] = p.status;
    }
  });

  const down = endpoints.filter(ep => ep.last_status === 'down').length;
  const up = endpoints.filter(ep => ep.last_status === 'up').length;
  const total = endpoints.length;

  return (
    <div style={styles.page}>
      <div style={styles.header}>
        <span style={styles.title}>ENDPOINT STATUS</span>
        <span style={styles.stats}>
          {up}/{total} UP
          {down > 0 && <span style={{ color: '#ff2020', marginLeft: 12 }}>{down} DOWN</span>}
        </span>
        <span style={styles.refreshTime}>{lastRefresh.toLocaleTimeString()}</span>
        <button style={styles.btn} onClick={refresh}>REFRESH</button>
        <button style={{ ...styles.btn, borderColor: '#00ff88', color: '#00ff88' }}
                onClick={() => setShowAddForm(f => !f)}>
          {showAddForm ? 'CANCEL' : '+ ADD'}
        </button>
      </div>

      {error && <div style={styles.error}>[ERROR] {error}</div>}

      {/* Add form */}
      {showAddForm && (
        <div style={styles.addForm}>
          <div style={styles.formTitle}>ADD ENDPOINT</div>
          <div style={styles.formRow}>
            <label style={styles.label}>Name</label>
            <input style={styles.input} value={newEndpoint.name}
              onChange={e => setNewEndpoint(f => ({ ...f, name: e.target.value }))} />
          </div>
          <div style={styles.formRow}>
            <label style={styles.label}>Host</label>
            <input style={styles.input} value={newEndpoint.host}
              onChange={e => setNewEndpoint(f => ({ ...f, host: e.target.value }))} />
          </div>
          <div style={styles.formRow}>
            <label style={styles.label}>Port</label>
            <input style={{ ...styles.input, width: 80 }} type="number" value={newEndpoint.port}
              onChange={e => setNewEndpoint(f => ({ ...f, port: Number(e.target.value) }))} />
          </div>
          <div style={styles.formRow}>
            <label style={styles.label}>Protocol</label>
            <select style={styles.select} value={newEndpoint.protocol}
              onChange={e => setNewEndpoint(f => ({ ...f, protocol: e.target.value }))}>
              <option>tcp</option>
              <option>mllp</option>
              <option>http</option>
              <option>https</option>
            </select>
          </div>
          <div style={styles.formRow}>
            <label style={styles.label}>Interval (s)</label>
            <input style={{ ...styles.input, width: 80 }} type="number"
              value={newEndpoint.check_interval_seconds}
              onChange={e => setNewEndpoint(f => ({ ...f, check_interval_seconds: Number(e.target.value) }))} />
          </div>
          <button style={styles.saveBtn} onClick={handleAdd}>SAVE</button>
        </div>
      )}

      {/* Endpoint table */}
      <div style={styles.tableWrap}>
        <table style={styles.table}>
          <thead>
            <tr>
              <th style={styles.th}>STATUS</th>
              <th style={styles.th}>NAME</th>
              <th style={styles.th}>HOST</th>
              <th style={styles.th}>PORT</th>
              <th style={styles.th}>PROTO</th>
              <th style={styles.th}>LATENCY</th>
              <th style={styles.th}>LAST CHECK</th>
              <th style={styles.th}>TAGS</th>
              <th style={styles.th}>ACTIONS</th>
            </tr>
          </thead>
          <tbody>
            {endpoints.map((ep) => {
              const status = ep.last_status || 'unknown';
              const color = STATUS_COLOR[status] || '#666';
              return (
                <tr key={ep.id} style={ep.last_status === 'down' ? styles.downRow : {}}>
                  <td style={styles.td}>
                    <span style={{ color, fontWeight: 'bold', letterSpacing: 1 }}>
                      {status.toUpperCase()}
                    </span>
                  </td>
                  <td style={styles.td}>{ep.name}</td>
                  <td style={styles.td}><code style={styles.code}>{ep.host}</code></td>
                  <td style={styles.td}><code style={styles.code}>{ep.port}</code></td>
                  <td style={styles.td}>{ep.protocol.toUpperCase()}</td>
                  <td style={styles.td}>
                    {ep.last_latency_ms != null
                      ? <span style={{ color: ep.last_latency_ms > 1000 ? '#ff8800' : '#aaa' }}>
                          {ep.last_latency_ms}ms
                        </span>
                      : '--'}
                  </td>
                  <td style={styles.td}>
                    {ep.last_checked_at ? _fmtDt(ep.last_checked_at) : '--'}
                  </td>
                  <td style={styles.td}>
                    {ep.tags.map((t: string) => (
                      <span key={t} style={styles.tag}>{t}</span>
                    ))}
                  </td>
                  <td style={styles.td}>
                    <button style={styles.actionBtn} onClick={() => loadHistory(ep.id)}>
                      HISTORY
                    </button>
                    <button style={{ ...styles.actionBtn, color: '#ff4444', marginLeft: 4 }}
                            onClick={() => handleDelete(ep.id)}>
                      DEL
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {endpoints.length === 0 && (
        <div style={styles.noData}>
          No endpoints configured. Click [+ ADD] to add your first MLLP/TCP endpoint.
        </div>
      )}

      {/* Probe history panel */}
      {selectedId && (
        <div style={styles.historyPanel}>
          <div style={styles.historyTitle}>
            PROBE HISTORY - {endpoints.find(e => e.id === selectedId)?.name}
            <button style={{ ...styles.actionBtn, marginLeft: 16 }}
                    onClick={() => setSelectedId(null)}>CLOSE</button>
          </div>
          <div style={styles.historyBar}>
            {history.slice(0, 60).map((p) => (
              <div key={p.id} title={`${p.status} ${p.latency_ms ?? '-'}ms ${_fmtDt(p.probed_at)}`}
                   style={{
                     width: 10, height: 28,
                     background: STATUS_COLOR[p.status] || '#333',
                     opacity: 0.8, marginRight: 1, cursor: 'help',
                   }} />
            ))}
          </div>
          <div style={{ color: '#555', fontSize: 11, marginTop: 4 }}>
            Left = most recent | Each bar = 1 probe
          </div>
        </div>
      )}
    </div>
  );
};

function _fmtDt(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
}

const styles: Record<string, React.CSSProperties> = {
  page: { background: '#0d0d0d', minHeight: '100vh', padding: 20, fontFamily: 'monospace' },
  header: { display: 'flex', alignItems: 'center', gap: 16, marginBottom: 20 },
  title: { color: '#4488ff', fontSize: 18, fontWeight: 'bold', letterSpacing: 2 },
  stats: { color: '#aaa', fontSize: 13, flex: 1 },
  refreshTime: { color: '#555', fontSize: 11 },
  btn: {
    background: 'none', border: '1px solid #333', color: '#666',
    padding: '4px 12px', fontSize: 12, cursor: 'pointer', fontFamily: 'monospace',
  },
  error: { background: '#1a0000', color: '#ff6666', padding: 10, marginBottom: 12,
            fontSize: 12, borderLeft: '4px solid #ff2020' },
  addForm: { background: '#111', border: '1px solid #222', padding: 16, marginBottom: 20 },
  formTitle: { color: '#00ff88', fontSize: 13, marginBottom: 14, letterSpacing: 1 },
  formRow: { display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 },
  label: { color: '#666', fontSize: 12, width: 100 },
  input: {
    background: '#0d0d0d', border: '1px solid #333', color: '#aaa',
    padding: '4px 8px', fontSize: 12, fontFamily: 'monospace', width: 200,
  },
  select: {
    background: '#0d0d0d', border: '1px solid #333', color: '#aaa',
    padding: '4px 8px', fontSize: 12, fontFamily: 'monospace',
  },
  saveBtn: {
    background: '#00ff88', border: 'none', color: '#000',
    padding: '6px 18px', fontSize: 12, cursor: 'pointer', fontFamily: 'monospace',
    fontWeight: 'bold', marginTop: 8,
  },
  tableWrap: { overflowX: 'auto' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
  th: {
    color: '#555', padding: '8px 12px', textAlign: 'left',
    borderBottom: '1px solid #1a1a1a', letterSpacing: 1, whiteSpace: 'nowrap',
  } as React.CSSProperties,
  td: { color: '#aaa', padding: '8px 12px', borderBottom: '1px solid #0d0d0d', whiteSpace: 'nowrap' },
  downRow: { background: '#1a0000' },
  code: { color: '#888', fontSize: 11 },
  tag: {
    background: '#1a1a2a', color: '#4488ff', padding: '1px 6px',
    fontSize: 10, borderRadius: 2, marginRight: 4,
  },
  actionBtn: {
    background: 'none', border: '1px solid #333', color: '#666',
    padding: '2px 8px', fontSize: 11, cursor: 'pointer', fontFamily: 'monospace',
  },
  noData: { color: '#555', fontSize: 13, padding: 24, border: '1px dashed #1a1a1a', marginTop: 16 },
  historyPanel: { marginTop: 24, padding: 16, background: '#111', border: '1px solid #1a1a1a' },
  historyTitle: { color: '#aaa', fontSize: 13, marginBottom: 12,
                   display: 'flex', alignItems: 'center' },
  historyBar: { display: 'flex', alignItems: 'flex-end', flexWrap: 'nowrap', overflowX: 'auto' },
};

export default EndpointStatus;
