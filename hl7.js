/**
 * HL7 v2.x Message Generator for Clinical Dictation
 *
 * Generates ORU^R01 (Observation Result) messages containing
 * transcribed clinical dictation that can be sent to a HIS / RIS.
 *
 * Designed for GE Centricity RIS compatibility:
 *   - Standard MSH|PID|PV1|ORC|OBR|OBX segment ordering
 *   - ORC segment included (GE RIS requires it for result routing)
 *   - HL7 v2.5 encoding with standard delimiters ^~\&
 *   - MLLP framing for TCP transport
 *   - No LIS changes required -- this app sends directly to RIS
 */

const SEGMENT_SEPARATOR = '\r';
const FIELD_SEPARATOR = '|';
const COMPONENT_SEPARATOR = '^';

/**
 * Report status codes per HL7 Table 0123
 *   D = Draft
 *   P = Preliminary
 *   F = Final
 *   A = Addendum
 */
const STATUS_MAP = {
  draft: 'D',
  preliminary: 'P',
  final: 'F',
  addendum: 'A',
};

function pad(n, width) {
  return String(n).padStart(width, '0');
}

function hl7Timestamp(date) {
  const d = date || new Date();
  return (
    d.getFullYear().toString() +
    pad(d.getMonth() + 1, 2) +
    pad(d.getDate(), 2) +
    pad(d.getHours(), 2) +
    pad(d.getMinutes(), 2) +
    pad(d.getSeconds(), 2)
  );
}

function generateControlId() {
  return 'DICT' + Date.now().toString(36).toUpperCase();
}

/**
 * Build MSH (Message Header) segment
 */
function buildMSH(sendingApp, sendingFacility, receivingApp, receivingFacility, controlId) {
  return [
    'MSH',
    '^~\\&',
    sendingApp || 'DICTAPHONE',
    sendingFacility || 'CLINIC',
    receivingApp || 'HIS',
    receivingFacility || 'HOSPITAL',
    hl7Timestamp(),
    '',
    'ORU^R01^ORU_R01',
    controlId,
    'P',
    '2.5',
  ].join(FIELD_SEPARATOR);
}

/**
 * Build PID (Patient Identification) segment
 */
function buildPID(patient) {
  return [
    'PID',
    '1',
    '',
    patient.id || '',
    '',
    [patient.lastName || '', patient.firstName || ''].join(COMPONENT_SEPARATOR),
    '',
    patient.dob || '',
    patient.gender || '',
  ].join(FIELD_SEPARATOR);
}

/**
 * Build PV1 (Patient Visit) segment
 */
function buildPV1(visit) {
  return [
    'PV1',
    '1',
    visit.patientClass || 'O',           // O = Outpatient
    visit.location || '',
    '',
    '',
    '',
    [visit.attendingId || '', visit.attendingLastName || '', visit.attendingFirstName || ''].join(COMPONENT_SEPARATOR),
  ].join(FIELD_SEPARATOR);
}

/**
 * Map report status to ORC order control code (HL7 Table 0119).
 * GE Centricity RIS uses ORC-1 to route results properly.
 */
const ORC_CONTROL_MAP = {
  draft: 'RE',        // Observations/Performed results to follow
  preliminary: 'RE',
  final: 'RE',
  addendum: 'RE',
};

/**
 * Map report status to ORC-5 order status (HL7 Table 0038).
 */
const ORC_STATUS_MAP = {
  draft: 'IP',        // In Process
  preliminary: 'IP',
  final: 'CM',        // Completed
  addendum: 'CM',
};

/**
 * Build ORC (Common Order) segment.
 * GE Centricity RIS requires ORC to properly match and file results.
 */
function buildORC(orderInfo, visit, status) {
  const now = hl7Timestamp();
  return [
    'ORC',
    ORC_CONTROL_MAP[status] || 'RE',                   // ORC-1  Order Control
    orderInfo.placerOrder || '',                         // ORC-2  Placer Order Number
    orderInfo.fillerOrder || '',                         // ORC-3  Filler Order Number
    '',                                                  // ORC-4  Placer Group Number
    ORC_STATUS_MAP[status] || 'CM',                     // ORC-5  Order Status
    '',                                                  // ORC-6
    '',                                                  // ORC-7
    '',                                                  // ORC-8
    now,                                                 // ORC-9  Date/Time of Transaction
    '',                                                  // ORC-10
    '',                                                  // ORC-11
    [visit.attendingId || '', visit.attendingLastName || '', visit.attendingFirstName || ''].join(COMPONENT_SEPARATOR), // ORC-12 Ordering Provider
  ].join(FIELD_SEPARATOR);
}

/**
 * Build OBR (Observation Request) segment
 */
function buildOBR(orderInfo, visit, status) {
  const now = hl7Timestamp();
  return [
    'OBR',
    '1',                                                 // OBR-1  Set ID
    orderInfo.placerOrder || '',                          // OBR-2  Placer Order Number
    orderInfo.fillerOrder || '',                          // OBR-3  Filler Order Number
    'DICT^Clinical Dictation',                           // OBR-4  Universal Service ID
    '',                                                  // OBR-5  Priority
    now,                                                 // OBR-6  Requested Date/Time
    now,                                                 // OBR-7  Observation Date/Time
    '',                                                  // OBR-8  Observation End Date/Time
    '',                                                  // OBR-9  Collection Volume
    '',                                                  // OBR-10 Collector Identifier
    '',                                                  // OBR-11 Specimen Action Code
    '',                                                  // OBR-12 Danger Code
    '',                                                  // OBR-13 Relevant Clinical Info
    '',                                                  // OBR-14 Specimen Received Date/Time
    '',                                                  // OBR-15 Specimen Source
    [visit.attendingId || '', visit.attendingLastName || '', visit.attendingFirstName || ''].join(COMPONENT_SEPARATOR), // OBR-16 Ordering Provider
    '',                                                  // OBR-17 Order Callback Phone
    '',                                                  // OBR-18 Placer Field 1
    '',                                                  // OBR-19 Placer Field 2
    '',                                                  // OBR-20 Filler Field 1
    '',                                                  // OBR-21 Filler Field 2
    now,                                                 // OBR-22 Results Rpt/Status Change
    '',                                                  // OBR-23 Charge to Practice
    '',                                                  // OBR-24 Diagnostic Serv Sect ID
    STATUS_MAP[status] || 'F',                           // OBR-25 Result Status
  ].join(FIELD_SEPARATOR);
}

/**
 * Build OBX (Observation Result) segments for the transcription text.
 * Long text is split across multiple OBX segments (max ~64k per segment).
 */
function buildOBX(text, status) {
  const MAX_LEN = 64000;
  const chunks = [];
  for (let i = 0; i < text.length; i += MAX_LEN) {
    chunks.push(text.slice(i, i + MAX_LEN));
  }
  if (chunks.length === 0) chunks.push('');

  return chunks.map((chunk, idx) => {
    return [
      'OBX',
      String(idx + 1),
      'TX',
      'TRANSCRIPTION^Dictation Text',
      String(idx + 1),
      chunk.replace(/\r/g, '\\X0D\\').replace(/\n/g, '\\X0A\\'),
      '',
      '',
      '',
      '',
      '',
      STATUS_MAP[status] || 'F',
    ].join(FIELD_SEPARATOR);
  });
}

/**
 * Build a complete ORU^R01 message from dictation data.
 *
 * @param {Object} opts
 * @param {Object} opts.patient       - { id, firstName, lastName, dob, gender }
 * @param {Object} opts.visit         - { patientClass, location, attendingId, attendingLastName, attendingFirstName }
 * @param {Object} opts.order         - { placerOrder, fillerOrder }
 * @param {string} opts.transcription - Dictated text
 * @param {string} opts.status        - draft | preliminary | final | addendum
 * @param {Object} opts.config        - { sendingApp, sendingFacility, receivingApp, receivingFacility }
 * @returns {{ raw: string, controlId: string }}
 */
function buildORU(opts) {
  const {
    patient = {},
    visit = {},
    order = {},
    transcription = '',
    status = 'final',
    config = {},
  } = opts;

  const controlId = generateControlId();

  const segments = [
    buildMSH(config.sendingApp, config.sendingFacility, config.receivingApp, config.receivingFacility, controlId),
    buildPID(patient),
    buildPV1(visit),
    buildORC(order, visit, status),
    buildOBR(order, visit, status),
    ...buildOBX(transcription, status),
  ];

  return {
    raw: segments.join(SEGMENT_SEPARATOR) + SEGMENT_SEPARATOR,
    controlId,
  };
}

/**
 * Wrap an HL7 message in MLLP (Minimal Lower Layer Protocol) framing.
 * MLLP is the standard TCP transport for HL7 v2 messages.
 *
 * Frame: <VT> message <FS><CR>
 */
function wrapMLLP(rawMessage) {
  const VT = String.fromCharCode(0x0b);  // Vertical Tab - start block
  const FS = String.fromCharCode(0x1c);  // File Separator - end block
  const CR = String.fromCharCode(0x0d);  // Carriage Return
  return VT + rawMessage + FS + CR;
}

/**
 * Parse an HL7 ACK response to determine acceptance.
 */
function parseACK(data) {
  const str = data.toString().replace(/[\x0b\x1c]/g, '');
  const segments = str.split(/\r|\n/).filter(Boolean);
  for (const seg of segments) {
    if (seg.startsWith('MSA')) {
      const fields = seg.split(FIELD_SEPARATOR);
      return {
        ackCode: fields[1] || '',   // AA = accepted, AE = error, AR = rejected
        controlId: fields[2] || '',
        text: fields[3] || '',
        accepted: fields[1] === 'AA',
      };
    }
  }
  return { ackCode: '', controlId: '', text: '', accepted: false };
}

module.exports = {
  buildORU,
  wrapMLLP,
  parseACK,
  STATUS_MAP,
  hl7Timestamp,
};
