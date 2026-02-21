/**
 * Worklist Query Module
 *
 * Queries GE Centricity RIS for the scheduled worklist using HL7 v2.5
 * ORM^O01 query or a lightweight MWL-style fetch so the doctor's phone
 * can display pending studies without manual patient entry.
 *
 * Two modes:
 *   1. MLLP query  – sends a QBP^Q11 to the RIS and parses the RSP^K11
 *   2. Local cache  – maintains a local worklist populated by inbound
 *                     ORM^O01 messages from the RIS (push model)
 *
 * The local cache is the zero-config option: the server opens an MLLP
 * listener that accepts order messages from RIS and stores them in memory
 * so the phone can browse the worklist instantly.
 */

const net = require('net');

const FIELD_SEP = '|';
const COMP_SEP = '^';

// ── In-memory worklist store ────────────────────────────────────
// Keyed by accession number, stores patient + study info.
const worklist = new Map();

function getWorklist() {
  return Array.from(worklist.values()).sort((a, b) => {
    return (b.scheduledTime || '').localeCompare(a.scheduledTime || '');
  });
}

function searchWorklist(query) {
  const q = (query || '').toLowerCase();
  if (!q) return getWorklist();
  return getWorklist().filter(item =>
    (item.patientId || '').toLowerCase().includes(q) ||
    (item.patientName || '').toLowerCase().includes(q) ||
    (item.accession || '').toLowerCase().includes(q) ||
    (item.studyDescription || '').toLowerCase().includes(q)
  );
}

function getWorklistItem(accession) {
  return worklist.get(accession) || null;
}

// ── Parse inbound ORM^O01 / OMG^O19 from RIS ───────────────────
function parseOrderMessage(raw) {
  const segs = raw.split(/\r|\n/).filter(Boolean);
  const parsed = {};
  for (const seg of segs) {
    const f = seg.split(FIELD_SEP);
    const name = f[0];
    if (name === 'MSH') {
      parsed.messageType = (f[8] || '').split(COMP_SEP)[0];
    } else if (name === 'PID') {
      const nameParts = (f[5] || '').split(COMP_SEP);
      parsed.patientId = f[3] || '';
      parsed.patientLastName = nameParts[0] || '';
      parsed.patientFirstName = nameParts[1] || '';
      parsed.patientName = [nameParts[0], nameParts[1]].filter(Boolean).join(', ');
      parsed.patientDob = f[7] || '';
      parsed.patientGender = f[8] || '';
    } else if (name === 'PV1') {
      parsed.patientClass = f[2] || 'O';
      parsed.location = f[3] || '';
      const drParts = (f[7] || '').split(COMP_SEP);
      parsed.attendingId = drParts[0] || '';
      parsed.attendingLastName = drParts[1] || '';
      parsed.attendingFirstName = drParts[2] || '';
    } else if (name === 'ORC') {
      parsed.placerOrder = f[2] || '';
      parsed.fillerOrder = f[3] || '';
    } else if (name === 'OBR') {
      parsed.accession = f[3] || parsed.fillerOrder || '';
      const svc = (f[4] || '').split(COMP_SEP);
      parsed.studyCode = svc[0] || '';
      parsed.studyDescription = svc[1] || svc[0] || '';
      parsed.scheduledTime = f[6] || '';
    }
  }
  return parsed;
}

function addToWorklist(entry) {
  if (!entry.accession) return;
  worklist.set(entry.accession, {
    accession: entry.accession,
    patientId: entry.patientId || '',
    patientName: entry.patientName || '',
    patientFirstName: entry.patientFirstName || '',
    patientLastName: entry.patientLastName || '',
    patientDob: entry.patientDob || '',
    patientGender: entry.patientGender || '',
    patientClass: entry.patientClass || 'O',
    location: entry.location || '',
    attendingId: entry.attendingId || '',
    attendingLastName: entry.attendingLastName || '',
    attendingFirstName: entry.attendingFirstName || '',
    placerOrder: entry.placerOrder || '',
    fillerOrder: entry.fillerOrder || entry.accession,
    studyCode: entry.studyCode || '',
    studyDescription: entry.studyDescription || '',
    scheduledTime: entry.scheduledTime || '',
    receivedAt: new Date().toISOString(),
    status: 'pending',
  });
}

// ── Build ACK response for inbound messages ─────────────────────
function buildACK(raw) {
  const segs = raw.split(/\r|\n/).filter(Boolean);
  let controlId = '';
  let sendingApp = '';
  let sendingFac = '';
  let recvApp = '';
  let recvFac = '';
  for (const seg of segs) {
    if (seg.startsWith('MSH')) {
      const f = seg.split(FIELD_SEP);
      sendingApp = f[2] || '';
      sendingFac = f[3] || '';
      recvApp = f[4] || '';
      recvFac = f[5] || '';
      controlId = f[9] || '';
      break;
    }
  }
  const now = new Date();
  const ts = now.getFullYear().toString() +
    String(now.getMonth() + 1).padStart(2, '0') +
    String(now.getDate()).padStart(2, '0') +
    String(now.getHours()).padStart(2, '0') +
    String(now.getMinutes()).padStart(2, '0') +
    String(now.getSeconds()).padStart(2, '0');

  const msh = ['MSH', '^~\\&', recvApp, recvFac, sendingApp, sendingFac, ts, '', 'ACK', 'ACK' + controlId, 'P', '2.5'].join(FIELD_SEP);
  const msa = ['MSA', 'AA', controlId].join(FIELD_SEP);
  return msh + '\r' + msa + '\r';
}

// ── MLLP Listener for inbound orders from RIS ──────────────────
function startWorklistListener(port, onMessage) {
  const VT = String.fromCharCode(0x0b);
  const FS = String.fromCharCode(0x1c);

  const server = net.createServer((socket) => {
    let buffer = '';

    socket.on('data', (data) => {
      buffer += data.toString();

      while (buffer.includes(FS)) {
        const endIdx = buffer.indexOf(FS);
        let msg = buffer.substring(0, endIdx);
        buffer = buffer.substring(endIdx + 2); // skip FS + CR

        // Strip VT start character
        if (msg.startsWith(VT)) msg = msg.substring(1);

        // Parse and store
        const entry = parseOrderMessage(msg);
        addToWorklist(entry);
        if (onMessage) onMessage(entry);

        // Send ACK wrapped in MLLP
        const ack = buildACK(msg);
        socket.write(VT + ack + FS + '\r');
      }
    });

    socket.on('error', () => {});
  });

  server.listen(port, () => {
    console.log(`Worklist MLLP listener on port ${port} (receives orders from RIS)`);
  });

  return server;
}

// ── Manual worklist entry (for testing / manual add) ────────────
function addManualEntry(data) {
  const accession = data.accession || 'ACC' + Date.now().toString(36).toUpperCase();
  addToWorklist({ ...data, accession });
  return accession;
}

module.exports = {
  getWorklist,
  searchWorklist,
  getWorklistItem,
  addToWorklist,
  addManualEntry,
  startWorklistListener,
  parseOrderMessage,
};
