import React from 'react';
import { Alert } from '../types';

interface AlertBannerProps {
  alerts: Alert[];
  onAcknowledge?: (id: string) => void;
  onViewBite?: (id: string) => void;
}

const SEV_STYLE: Record<string, React.CSSProperties> = {
  critical: { background: '#1a0000', borderLeft: '4px solid #ff2020', color: '#ff6666' },
  warning: { background: '#1a0e00', borderLeft: '4px solid #ff8800', color: '#ffaa44' },
  info: { background: '#00101a', borderLeft: '4px solid #4488ff', color: '#88aaff' },
};

const AlertBanner: React.FC<AlertBannerProps> = ({ alerts, onAcknowledge, onViewBite }) => {
  if (!alerts.length) return null;

  return (
    <div style={styles.container}>
      {alerts.map((a) => (
        <div key={a.id} style={{ ...styles.item, ...(SEV_STYLE[a.severity] || SEV_STYLE.info) }}>
          <div style={styles.header}>
            <span style={styles.badge}>{a.severity.toUpperCase()}</span>
            <span style={styles.type}>[{a.alert_type}]</span>
            <span style={styles.title}>{a.title}</span>
            <span style={styles.age}>{_ago(a.created_at)}</span>
          </div>
          {a.reason_code && (
            <div style={styles.reason}>Reason: {a.reason_code}</div>
          )}
          <div style={styles.actions}>
            {onViewBite && (
              <button style={styles.btn} onClick={() => onViewBite(a.id)}>
                VIEW BITE
              </button>
            )}
            {onAcknowledge && a.status === 'open' && (
              <button style={styles.btn} onClick={() => onAcknowledge(a.id)}>
                ACK
              </button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
};

function _ago(iso: string): string {
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

const styles: Record<string, React.CSSProperties> = {
  container: { display: 'flex', flexDirection: 'column', gap: 4, fontFamily: 'monospace' },
  item: { padding: '10px 14px', fontSize: 12 },
  header: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' },
  badge: { fontWeight: 'bold', fontSize: 11, letterSpacing: 1 },
  type: { color: '#888', fontSize: 11 },
  title: { flex: 1, fontWeight: 'bold' },
  age: { color: '#888', fontSize: 11 },
  reason: { marginTop: 4, color: '#888', fontSize: 11 },
  actions: { marginTop: 8, display: 'flex', gap: 8 },
  btn: {
    background: 'none', border: '1px solid #444', color: '#aaa',
    padding: '3px 10px', fontSize: 11, cursor: 'pointer', fontFamily: 'monospace',
  },
};

export default AlertBanner;
