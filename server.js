/**
 * Clinical Dictaphone HL7 Server
 *
 * Mobile-first dictaphone replacement for clinical use.
 * Doctors use their iPhone/Android as a dictaphone:
 *   1. Phone connects to this server over WiFi
 *   2. Worklist is populated from RIS via inbound HL7 orders (MLLP listener)
 *   3. Doctor selects a patient/study, dictates, sets report status
 *   4. Transcription is sent as HL7 ORU^R01 directly to GE Centricity RIS
 *
 * No LIS changes required -- bypasses traditional dictaphone + LIS workflow.
 */

const express = require('express');
const net = require('net');
const path = require('path');
const { buildORU, wrapMLLP, parseACK } = require('./hl7');
const { getWorklist, searchWorklist, getWorklistItem, addManualEntry, startWorklistListener } = require('./worklist');

const app = express();
const PORT = process.env.PORT || 3000;
const WORKLIST_PORT = process.env.WORKLIST_PORT || 2576;

app.use(express.json({ limit: '2mb' }));
app.use(express.static(path.join(__dirname, 'public')));

// ── Worklist endpoints ──────────────────────────────────────────

// GET /api/worklist?q=search
app.get('/api/worklist', (req, res) => {
  const q = req.query.q || '';
  res.json(q ? searchWorklist(q) : getWorklist());
});

// GET /api/worklist/:accession
app.get('/api/worklist/:accession', (req, res) => {
  const item = getWorklistItem(req.params.accession);
  if (!item) return res.status(404).json({ error: 'Not found' });
  res.json(item);
});

// POST /api/worklist  (manual add)
app.post('/api/worklist', (req, res) => {
  const { patientId, firstName, lastName, dob, gender, studyDescription, accession } = req.body;
  if (!patientId || !lastName) {
    return res.status(400).json({ error: 'patientId and lastName are required.' });
  }
  const acc = addManualEntry({
    patientId,
    patientFirstName: firstName || '',
    patientLastName: lastName || '',
    patientName: [lastName, firstName].filter(Boolean).join(', '),
    patientDob: dob || '',
    patientGender: gender || '',
    studyDescription: studyDescription || 'Clinical Dictation',
    accession: accession || undefined,
  });
  res.json({ accession: acc });
});

// ── Preview endpoint ────────────────────────────────────────────
app.post('/api/preview', (req, res) => {
  try {
    const msg = buildORU(req.body);
    res.json({ raw: msg.raw, controlId: msg.controlId });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

// ── Send endpoint (MLLP over TCP) ──────────────────────────────
app.post('/api/send', (req, res) => {
  const { hl7Host, hl7Port } = req.body;
  if (!hl7Host || !hl7Port) {
    return res.status(400).json({ success: false, error: 'HL7 host and port are required.' });
  }

  let msg;
  try {
    msg = buildORU(req.body);
  } catch (err) {
    return res.status(400).json({ success: false, error: err.message });
  }

  const frame = wrapMLLP(msg.raw);
  const client = new net.Socket();
  let responded = false;
  let ackBuffer = '';

  const timeout = setTimeout(() => {
    if (!responded) {
      responded = true;
      client.destroy();
      res.json({ success: false, error: 'Connection timed out (10 s).' });
    }
  }, 10000);

  client.connect(hl7Port, hl7Host, () => {
    client.write(frame);
  });

  client.on('data', (data) => {
    ackBuffer += data.toString();
    if (ackBuffer.includes(String.fromCharCode(0x1c))) {
      clearTimeout(timeout);
      const ack = parseACK(ackBuffer);
      if (!responded) {
        responded = true;
        client.destroy();
        res.json({
          success: ack.accepted,
          ackCode: ack.ackCode,
          controlId: msg.controlId,
          ackText: ack.text,
        });
      }
    }
  });

  client.on('error', (err) => {
    clearTimeout(timeout);
    if (!responded) {
      responded = true;
      client.destroy();
      res.json({ success: false, error: err.message });
    }
  });

  client.on('close', () => {
    clearTimeout(timeout);
    if (!responded) {
      responded = true;
      res.json({ success: false, error: 'Connection closed without ACK.' });
    }
  });
});

// ── Start ───────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`Dictaphone server: http://localhost:${PORT}`);
  console.log(`Open this URL on your phone (same WiFi network).`);
});

// Start MLLP listener for inbound orders from RIS
startWorklistListener(WORKLIST_PORT, (entry) => {
  console.log(`Worklist +  ${entry.patientName || 'Unknown'} / ${entry.accession || '--'}`);
});
