/**
 * Clinical Dictaphone HL7 Server
 *
 * Serves the dictaphone web UI and provides API endpoints to:
 *   - Preview an HL7 ORU^R01 message
 *   - Transmit the message over MLLP/TCP to a HIS / RIS (GE Centricity compatible)
 *
 * The HL7 messages are generated with standard ORU^R01 structure that is
 * directly consumable by GE Centricity RIS without requiring changes to
 * any LIS middleware -- the dictaphone acts as a standalone sending
 * application that pushes results directly into the RIS.
 */

const express = require('express');
const net = require('net');
const path = require('path');
const { buildORU, wrapMLLP, parseACK } = require('./hl7');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json({ limit: '2mb' }));
app.use(express.static(path.join(__dirname, 'public')));

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
    // Check for MLLP end-of-block (FS CR)
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
      // If connection closed with no ACK, treat as failure
      res.json({ success: false, error: 'Connection closed without ACK.' });
    }
  });
});

// ── Start ───────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`Dictaphone HL7 server running on http://localhost:${PORT}`);
});
