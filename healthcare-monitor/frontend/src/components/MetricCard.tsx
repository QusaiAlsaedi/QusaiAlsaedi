import React from 'react';

interface MetricCardProps {
  title: string;
  value: string | number;
  subValue?: string;
  status?: 'ok' | 'warn' | 'critical' | 'info' | 'unknown';
  wide?: boolean;
}

const STATUS_COLORS: Record<string, string> = {
  ok: '#00ff88',
  warn: '#ff8800',
  critical: '#ff2020',
  info: '#4488ff',
  unknown: '#888888',
};

const MetricCard: React.FC<MetricCardProps> = ({
  title, value, subValue, status = 'ok', wide
}) => {
  const color = STATUS_COLORS[status] || '#00ff88';
  return (
    <div style={{ ...styles.card, width: wide ? 240 : 160 }}>
      <div style={styles.title}>{title}</div>
      <div style={{ ...styles.value, color }}>{value}</div>
      {subValue && <div style={styles.sub}>{subValue}</div>}
      <div style={{ ...styles.indicator, background: color }} />
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  card: {
    background: '#111', border: '1px solid #222', padding: 16,
    fontFamily: 'monospace', position: 'relative', minHeight: 90,
  },
  title: { color: '#666', fontSize: 11, letterSpacing: 2, marginBottom: 8, textTransform: 'uppercase' },
  value: { fontSize: 28, fontWeight: 'bold', lineHeight: 1 },
  sub: { color: '#666', fontSize: 11, marginTop: 6 },
  indicator: { position: 'absolute', bottom: 0, left: 0, right: 0, height: 2 },
};

export default MetricCard;
