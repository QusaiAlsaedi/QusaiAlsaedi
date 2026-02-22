/**
 * Overview Dashboard - shows all subsystem health at a glance.
 * Auto-refreshes every 30 seconds.
 */
import React, { useEffect, useState, useCallback } from 'react';
import MetricCard from '../components/MetricCard';
import AlertBanner from '../components/AlertBanner';
import { dashboardApi, alertsApi } from '../api/client';
import { DashboardSummary, Alert, ServerSummary } from '../types';
import { metricsApi } from '../api/client';

const REFRESH_MS = 30_000;

const Overview: React.FC = () => {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [servers, setServers] = useState<ServerSummary[]>([]);
  const [biteModal, setBiteModal] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());

  const refresh = useCallback(async () => {
    try {
      const [sumRes, alertRes, srvRes] = await Promise.all([
        dashboardApi.summary(),
        alertsApi.list({ status: 'open', limit: 10 }),
        metricsApi.servers(),
      ]);
      setSummary(sumRes.data);
      setAlerts(alertRes.data);
      setServers(srvRes.data);
      setLastRefresh(new Date());
      setError(null);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e.message || 'Failed to load');
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const handleAck = async (id: string) => {
    await alertsApi.acknowledge(id);
    await refresh();
  };

  const handleViewBite = async (id: string) => {
    const res = await alertsApi.bite(id);
    setBiteModal(res.data.bite_text);
  };

  const ep = summary?.endpoints;
  const srv = summary?.servers;
  const hl7 = summary?.hl7;
  const al = summary?.alerts;

  const critAlerts = alerts.filter(a => a.severity === 'critical');
  const warnAlerts = alerts.filter(a => a.severity === 'warning');
  const displayAlerts = [...critAlerts, ...warnAlerts].slice(0, 10);

  return (
    <div style={styles.page}>
      <div style={styles.header}>
        <span style={styles.title}>SYSTEM OVERVIEW</span>
        <span style={styles.refresh}>Last refresh: {lastRefresh.toLocaleTimeString()}</span>
        <button style={styles.refreshBtn} onClick={refresh}>REFRESH</button>
      </div>

      {error && <div style={styles.error}>[ERROR] {error}</div>}

      {/* Active Alerts */}
      {displayAlerts.length > 0 && (
        <section style={styles.section}>
          <div style={styles.sectionTitle}>ACTIVE ALERTS</div>
          <AlertBanner
            alerts={displayAlerts}
            onAcknowledge={handleAck}
            onViewBite={handleViewBite}
          />
        </section>
      )}

      {/* Endpoint Health */}
      <section style={styles.section}>
        <div style={styles.sectionTitle}>NETWORK ENDPOINTS</div>
        <div style={styles.cards}>
          <MetricCard title="TOTAL" value={ep?.total ?? '-'}
            status="info" />
          <MetricCard title="UP" value={ep?.up ?? '-'}
            status={ep?.down === 0 ? 'ok' : 'warn'} />
          <MetricCard title="DOWN" value={ep?.down ?? '-'}
            status={(ep?.down ?? 0) > 0 ? 'critical' : 'ok'} />
          <MetricCard title="UNKNOWN" value={ep?.unknown ?? '-'}
            status="unknown" />
        </div>
      </section>

      {/* HL7 Flow */}
      <section style={styles.section}>
        <div style={styles.sectionTitle}>HL7 MESSAGE FLOW (LAST 1H)</div>
        <div style={styles.cards}>
          <MetricCard title="ACKED" value={hl7?.acked_1h ?? '-'} status="ok" />
          <MetricCard title="STUCK >30s" value={hl7?.stuck_30s ?? '-'}
            status={(hl7?.stuck_30s ?? 0) > 0 ? 'critical' : 'ok'} />
          <MetricCard title="PENDING" value={hl7?.pending ?? '-'}
            status={(hl7?.pending ?? 0) > 10 ? 'warn' : 'ok'} />
          <MetricCard title="NACKED" value={hl7?.nacked_1h ?? '-'}
            status={(hl7?.nacked_1h ?? 0) > 0 ? 'warn' : 'ok'} />
          <MetricCard title="TIMEOUT" value={hl7?.timeout_1h ?? '-'}
            status={(hl7?.timeout_1h ?? 0) > 0 ? 'critical' : 'ok'} />
        </div>
      </section>

      {/* Server Utilization */}
      <section style={styles.section}>
        <div style={styles.sectionTitle}>SERVER UTILIZATION</div>
        {servers.length === 0 && (
          <div style={styles.noData}>No server metrics received. Is the agent running?</div>
        )}
        <div style={styles.serverGrid}>
          {servers.map((s) => (
            <div key={s.server_name} style={{ ...styles.serverCard, ...serverBorderColor(s.status) }}>
              <div style={styles.serverName}>{s.server_name}</div>
              <div style={styles.serverPlatform}>[{s.platform || 'unknown'}]</div>
              <div style={styles.serverRow}>
                <span style={styles.serverLabel}>CPU</span>
                <span style={gaugeStyle(s.cpu_percent, 85, 95)}>
                  {s.cpu_percent != null ? `${s.cpu_percent.toFixed(1)}%` : '--'}
                </span>
              </div>
              <div style={styles.serverRow}>
                <span style={styles.serverLabel}>MEM</span>
                <span style={gaugeStyle(s.memory_percent, 85, 95)}>
                  {s.memory_percent != null ? `${s.memory_percent.toFixed(1)}%` : '--'}
                </span>
              </div>
              <div style={styles.serverRow}>
                <span style={styles.serverLabel}>DISK</span>
                <span style={gaugeStyle(s.disk_percent, 85, 95)}>
                  {s.disk_percent != null ? `${s.disk_percent.toFixed(1)}%` : '--'}
                </span>
              </div>
              {Object.entries(s.services || {}).map(([svc, st]) => (
                <div key={svc} style={styles.serverRow}>
                  <span style={styles.serverLabel}>{svc.substring(0, 12).toUpperCase()}</span>
                  <span style={{ color: st === 'running' ? '#00ff88' : '#ff2020', fontSize: 12 }}>
                    {st.toUpperCase()}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
      </section>

      {/* Alert count badges */}
      <section style={styles.section}>
        <div style={styles.sectionTitle}>OPEN ALERTS</div>
        <div style={styles.cards}>
          <MetricCard title="CRITICAL" value={al?.open_critical ?? 0}
            status={(al?.open_critical ?? 0) > 0 ? 'critical' : 'ok'} />
          <MetricCard title="WARNING" value={al?.open_warning ?? 0}
            status={(al?.open_warning ?? 0) > 0 ? 'warn' : 'ok'} />
          <MetricCard title="INFO" value={al?.open_info ?? 0} status="info" />
        </div>
      </section>

      {/* BITE modal */}
      {biteModal && (
        <div style={styles.overlay} onClick={() => setBiteModal(null)}>
          <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
            <div style={styles.modalTitle}>BITE WARNING</div>
            <pre style={styles.biteText}>{biteModal}</pre>
            <button style={styles.closeBtn} onClick={() => setBiteModal(null)}>CLOSE</button>
          </div>
        </div>
      )}
    </div>
  );
};

function gaugeStyle(val: number | null, warn: number, crit: number): React.CSSProperties {
  if (val == null) return { color: '#666', fontSize: 13, fontFamily: 'monospace' };
  const color = val >= crit ? '#ff2020' : val >= warn ? '#ff8800' : '#00ff88';
  return { color, fontSize: 13, fontFamily: 'monospace', fontWeight: 'bold' };
}

function serverBorderColor(status: string): React.CSSProperties {
  const map: Record<string, string> = {
    healthy: '#1a2a1a', warning: '#2a1a00', critical: '#2a0000', unknown: '#1a1a1a'
  };
  return { borderColor: map[status] || '#222' };
}

const styles: Record<string, React.CSSProperties> = {
  page: { background: '#0d0d0d', minHeight: '100vh', padding: 20, fontFamily: 'monospace' },
  header: { display: 'flex', alignItems: 'center', gap: 16, marginBottom: 24 },
  title: { color: '#00ff88', fontSize: 18, fontWeight: 'bold', letterSpacing: 2, flex: 1 },
  refresh: { color: '#555', fontSize: 11 },
  refreshBtn: {
    background: 'none', border: '1px solid #333', color: '#666',
    padding: '4px 12px', fontSize: 12, cursor: 'pointer', fontFamily: 'monospace',
  },
  error: { background: '#1a0000', color: '#ff6666', padding: '10px 14px', marginBottom: 16,
            fontSize: 12, borderLeft: '4px solid #ff2020' },
  section: { marginBottom: 28 },
  sectionTitle: { color: '#555', fontSize: 11, letterSpacing: 3, marginBottom: 12,
                   textTransform: 'uppercase', borderBottom: '1px solid #1a1a1a', paddingBottom: 4 },
  cards: { display: 'flex', gap: 12, flexWrap: 'wrap' },
  noData: { color: '#555', fontSize: 12, padding: 16, border: '1px dashed #222' },
  serverGrid: { display: 'flex', gap: 12, flexWrap: 'wrap' },
  serverCard: {
    background: '#111', border: '1px solid #222', padding: 14,
    minWidth: 200, fontFamily: 'monospace',
  },
  serverName: { color: '#00ff88', fontSize: 14, fontWeight: 'bold', marginBottom: 4 },
  serverPlatform: { color: '#555', fontSize: 10, marginBottom: 10 },
  serverRow: { display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                marginBottom: 4, gap: 12 },
  serverLabel: { color: '#555', fontSize: 11, letterSpacing: 1 },
  overlay: {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.85)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
  },
  modal: {
    background: '#0d0d0d', border: '1px solid #333', padding: 24,
    maxWidth: '80vw', maxHeight: '80vh', overflow: 'auto', fontFamily: 'monospace',
  },
  modalTitle: { color: '#ff2020', fontSize: 14, fontWeight: 'bold', marginBottom: 16 },
  biteText: { color: '#00ff88', fontSize: 12, whiteSpace: 'pre', lineHeight: 1.5, margin: 0 },
  closeBtn: {
    marginTop: 16, background: 'none', border: '1px solid #444', color: '#888',
    padding: '6px 18px', cursor: 'pointer', fontFamily: 'monospace',
  },
};

export default Overview;
