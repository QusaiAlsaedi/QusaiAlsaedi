# Mobile Dictation Agent — End-to-End Sequence Diagrams

## 2. End-to-End Sequences

Legend:
```
─►  synchronous call / message
--► asynchronous / event-driven
═►  persisted / durable write
```

---

### 2a. Start Session: Centricity Launch → Agent Context → Phone Pairing

```
Radiologist    Centricity RIS    Windows Agent          Phone App
    │                │                │                     │
    │  Open exam &   │                │                     │
    │  click Dictate │                │                     │
    │───────────────►│                │                     │
    │                │  Launch Agent  │                     │
    │                │  (CLI args):   │                     │
    │                │  /mrn=MRN123   │                     │
    │                │  /acc=ACC456   │                     │
    │                │  /pt=DOE,JOHN  │                     │
    │                │  /dob=19500101 │                     │
    │                │  /sex=M        │                     │
    │                │  /mod=CT       │                     │
    │                │  /proc=CT Chest│                     │
    │                │────────────────►                     │
    │                │                │                     │
    │                │ [CCOW also     │                     │
    │                │  publishes     │                     │
    │                │  context items]│                     │
    │                │ ─ ─ ─ ─ ─ ─ ─►│                     │
    │                │                │ ContextProvider:    │
    │                │                │ parse + validate    │
    │                │                │ ClinicalContext     │
    │                │                │                     │
    │                │                │ SessionManager:     │
    │                │                │ create Session{     │
    │                │                │   id: uuid,         │
    │                │                │   winUser: DOMAIN\rad│
    │                │                │   context: {...}    │
    │                │                │   state: WAITING    │
    │                │                │ }                   │
    │                │                │                     │
    │                │                │ DeviceManager:      │
    │                │                │ generate PairToken  │
    │                │                │ (HMAC-SHA256,60s TTL│
    │                │                │  bound to session+  │
    │                │                │  winUser)           │
    │                │                │                     │
    │                │                │ Render QR on screen │
    │                │                │ (system tray popup  │
    │                │                │  or small window):  │
    │                │                │  QR = HTTPS URL +   │
    │                │                │  token + Agent IP   │
    │◄───────────────────────────────│                     │
    │  [QR appears on PC screen]     │                     │
    │                │                │                     │
    │  Scan QR with phone             │                     │
    │────────────────────────────────────────────────────►  │
    │                │                │                     │ Parse QR:
    │                │                │                     │ endpoint = wss://192.168.1.10:8443
    │                │                │                     │ token    = eyJ...
    │                │                │                     │
    │                │                │◄────────────────────│
    │                │                │  WSS Connect        │
    │                │                │  POST /pair         │
    │                │                │  {token, deviceId,  │
    │                │                │   deviceOS,         │
    │                │                │   publicKey(ECDH)}  │
    │                │                │                     │
    │                │                │ DeviceManager:      │
    │                │                │ validate token TTL  │
    │                │                │ verify not replayed │
    │                │                │ bind deviceId →     │
    │                │                │   sessionId         │
    │                │                │ issue SessionToken  │
    │                │                │ (JWT, 8h, HS256)    │
    │                │                │ ECDH key exchange   │
    │                │                │ (E2E channel key)   │
    │                │                │                     │
    │                │                │─────────────────────►
    │                │                │  PairResponse{      │
    │                │                │    sessionToken,    │
    │                │                │    context: {...},  │
    │                │                │    serverPubKey     │
    │                │                │  }                  │
    │                │                │                     │
    │                │                │ AuditLog: PAIR_OK   │
    │                │                │ MRN=xxxx1234        │
    │                │                │ device=iPhone-uuid  │
    │                │                │                     │
    │                │                │                     │ Phone renders:
    │                │                │                     │ Patient header
    │                │                │                     │ Case details
    │                │                │                     │ Workflow buttons
    │                │                │                     │ Status: READY
    │◄────────────────────────────────────────────────────  │
    │  [Phone shows patient info + workflow UI]             │
```

---

### 2b. Read → Draft Workflow

```
Radiologist    Phone App         Windows Agent       HL7 Interface
    │               │                 │                   │
    │  Tap [READ]   │                 │                   │
    │──────────────►│                 │                   │
    │               │  WS: WorkflowAction{                │
    │               │    action: "READ",                  │
    │               │    sessionToken: "eyJ..."          │
    │               │  }                                  │
    │               │────────────────►│                   │
    │               │                 │ SessionManager:   │
    │               │                 │ validate token    │
    │               │                 │ SafetyGuard:      │
    │               │                 │ context unchanged?│
    │               │                 │ → YES             │
    │               │                 │ set state=READING  │
    │               │                 │ AuditLog:         │
    │               │                 │ READ_STARTED      │
    │               │                 │                   │
    │               │◄────────────────│                   │
    │               │  Ack{state:     │                   │
    │               │   "READING"}    │                   │
    │               │                 │                   │
    │  Tap [START   │                 │                   │
    │   DICTATION]  │                 │                   │
    │──────────────►│                 │                   │
    │               │  WS: StartDictation{                │
    │               │    sessionToken,                    │
    │               │    audioFormat: "opus",             │
    │               │    sampleRate: 16000                │
    │               │  }                                  │
    │               │────────────────►│                   │
    │               │                 │ SessionManager:   │
    │               │                 │ open audio channel│
    │               │                 │ set state=DICTATING│
    │               │                 │                   │
    │               │◄────────────────│                   │
    │               │  Ack{           │                   │
    │               │  dictationId,   │                   │
    │               │  state:"DICTATING"}                 │
    │               │                 │                   │
    │  [Dictates    │                 │                   │
    │   into phone] │                 │                   │
    │               │ AudioChunk x N  │                   │
    │               │────────────────►│                   │
    │               │ (WS binary      │                   │
    │               │  frames, 250ms) │                   │
    │               │                 │ Buffer audio      │
    │               │                 │ (Phase 1: file)   │
    │               │                 │ (Phase 3: STT)    │
    │               │                 │                   │
    │  Tap [STOP    │                 │                   │
    │   DICTATION]  │                 │                   │
    │──────────────►│                 │                   │
    │               │  WS: StopDictation{                 │
    │               │    sessionToken,                    │
    │               │    dictationId                      │
    │               │  }                                  │
    │               │────────────────►│                   │
    │               │                 │ finalize audio    │
    │               │                 │ (Phase 1: save    │
    │               │                 │  .opus file,      │
    │               │                 │  hand to Centricity│
    │               │                 │  or transcription)│
    │               │                 │                   │
    │               │                 │ Hl7OruBuilder:    │
    │               │                 │ build ORU^R01     │
    │               │                 │ OBR-25 = P (DRAFT)│
    │               │                 │                   │
    │               │                 │ OutboundQueue:    │
    │               │                 │ encrypt + persist │
    │               │                 │ ═══════════════   │
    │               │                 │                   │
    │               │                 │ Hl7Sender:        │
    │               │                 │ send via MLLP/drop│
    │               │                 │────────────────────►
    │               │                 │                   │ HL7 ACK
    │               │                 │◄────────────────── │
    │               │                 │ dequeue message   │
    │               │                 │ AuditLog:ORU_SENT  │
    │               │                 │ state=DRAFT       │
    │               │◄────────────────│                   │
    │               │ StatusUpdate{   │                   │
    │               │  status:"DRAFT" │                   │
    │               │ }               │                   │
    │◄──────────────│                 │                   │
    │  [Phone shows │                 │                   │
    │   DRAFT badge]│                 │                   │
```

---

### 2c. Co-read Workflow

```
Radiologist    Phone App         Windows Agent       HL7 Interface
    │               │                 │                   │
    │ (Session is   │                 │                   │
    │  in DRAFT     │                 │                   │
    │  state)       │                 │                   │
    │               │                 │                   │
    │  Tap [CO-READ]│                 │                   │
    │──────────────►│                 │                   │
    │               │  WS: WorkflowAction{                │
    │               │    action: "CO_READ",               │
    │               │    sessionToken                     │
    │               │  }                                  │
    │               │────────────────►│                   │
    │               │                 │ SafetyGuard:      │
    │               │                 │ state must be     │
    │               │                 │ DRAFT or READING  │
    │               │                 │ context unchanged?│
    │               │                 │ set state=        │
    │               │                 │ SECOND_READ       │
    │               │                 │                   │
    │               │◄────────────────│                   │
    │               │ Ack{state:      │                   │
    │               │ "SECOND_READ_   │                   │
    │               │  PENDING"}      │                   │
    │               │                 │                   │
    │  [2nd rad     │                 │                   │
    │   dictates]   │                 │                   │
    │  Tap START /  │                 │                   │
    │  STOP (same   │                 │                   │
    │  as 2b above) │                 │                   │
    │               │                 │                   │
    │               │                 │ Hl7OruBuilder:    │
    │               │                 │ build ORU^R01     │
    │               │                 │ OBR-25 = P        │
    │               │                 │ add OBX for co-   │
    │               │                 │ read annotation   │
    │               │                 │────────────────────►
    │               │                 │◄──────────────────│
    │               │◄────────────────│                   │
    │               │ StatusUpdate{   │                   │
    │               │ status:"SECOND_ │                   │
    │               │  READ_PENDING"} │                   │
    │◄──────────────│                 │                   │
    │ [Phone shows  │                 │                   │
    │ SECOND_READ_  │                 │                   │
    │ PENDING badge]│                 │                   │
```

---

### 2d. Approve → Final → ORU Sent + ACK Received

```
Radiologist    Phone App         Windows Agent       HL7 Interface
    │               │                 │                   │
    │ (Report text  │                 │                   │
    │  complete,    │                 │                   │
    │  status=DRAFT │                 │                   │
    │  or SECOND_   │                 │                   │
    │  READ_PENDING)│                 │                   │
    │               │                 │                   │
    │  Tap [APPROVE]│                 │                   │
    │──────────────►│                 │                   │
    │               │  WS: WorkflowAction{                │
    │               │    action: "APPROVE",               │
    │               │    sessionToken,                    │
    │               │    confirmOverride: false           │
    │               │  }                                  │
    │               │────────────────►│                   │
    │               │                 │ SafetyGuard.      │
    │               │                 │ ValidateApprove():│
    │               │                 │                   │
    │               │                 │ Check 1: MRN      │
    │               │                 │ unchanged since   │
    │               │                 │ session start?    │
    │               │                 │ → PASS            │
    │               │                 │                   │
    │               │                 │ Check 2: Accession│
    │               │                 │ unchanged?        │
    │               │                 │ → PASS            │
    │               │                 │                   │
    │               │                 │ Check 3: state ∈  │
    │               │                 │ {DRAFT,SECOND_READ│
    │               │                 │ _PENDING}?        │
    │               │                 │ → PASS            │
    │               │                 │                   │
    │               │                 │ Check 4: dictation│
    │               │                 │ audio present?    │
    │               │                 │ → PASS            │
    │               │                 │                   │
    │               │                 │ Hl7OruBuilder:    │
    │               │                 │ build ORU^R01     │
    │               │                 │ OBR-25 = F (FINAL)│
    │               │                 │ fill {REPORT_TEXT}│
    │               │                 │ fill {TIMESTAMP}  │
    │               │                 │ fill {APPROVER}   │
    │               │                 │                   │
    │               │                 │ OutboundQueue:    │
    │               │                 │ AES-256-GCM write │
    │               │                 │ ═══════════════   │
    │               │                 │                   │
    │               │                 │ Hl7Sender attempt 1:
    │               │                 │────────────────────►
    │               │                 │                   │
    │               │                 │                   │ Process ORU
    │               │                 │                   │ return ACK
    │               │                 │◄──────────────────│
    │               │                 │ ACK: AA           │
    │               │                 │ dequeue message   │
    │               │                 │ AuditLog:         │
    │               │                 │ FINAL_APPROVED    │
    │               │                 │ ORU_ACK_RECEIVED  │
    │               │                 │ state=FINAL       │
    │               │◄────────────────│                   │
    │               │ StatusUpdate{   │                   │
    │               │  status:"FINAL",│                   │
    │               │  ackCode:"AA"   │                   │
    │               │ }               │                   │
    │◄──────────────│                 │                   │
    │ [FINAL shown, │                 │                   │
    │  green check] │                 │                   │
    │               │                 │                   │
    │               │                 │ SessionManager:   │
    │               │                 │ close session     │
    │               │                 │ (phone may re-pair│
    │               │                 │  for next exam)   │

── NAK / Retry sub-sequence (if ACK not received) ──────────

    │               │                 │ ACK: AE or timeout│
    │               │                 │ retry attempt 2   │
    │               │                 │ (wait 5s)         │
    │               │                 │────────────────────►
    │               │                 │ ... retry 3 (30s) │
    │               │                 │ ... retry 4 (120s)│
    │               │                 │────────────────────►
    │               │                 │ NAK persists:     │
    │               │                 │ move to dead-letter│
    │               │                 │ queue             │
    │               │                 │ ALERT: tray notif │
    │               │◄────────────────│                   │
    │               │ ErrorNotification│                  │
    │               │ {code:"HL7_SEND_│                   │
    │               │  FAILED",       │                   │
    │               │  retryable:true}│                   │
    │◄──────────────│                 │                   │
    │ [WARNING:     │                 │                   │
    │  HL7 send     │                 │                   │
    │  failed -     │                 │                   │
    │  IT notified] │                 │                   │
```

---

### 2e. Context Change Mid-Dictation (Safety Stop)

```
Radiologist    Centricity RIS    Windows Agent      Phone App
    │                │                │                  │
    │ (Dictation     │                │                  │
    │  in progress,  │                │                  │
    │  state=DICTATING│               │                  │
    │  MRN=MRN123)   │                │                  │
    │                │                │                  │
    │ Accidentally   │                │                  │
    │ clicks on a    │                │                  │
    │ different exam │                │                  │
    │───────────────►│                │                  │
    │                │ CCOW publishes │                  │
    │                │ new context:   │                  │
    │                │ MRN=MRN999     │                  │
    │                │ ACC=ACC777     │                  │
    │                │────────────────►                  │
    │                │                │ ContextProvider: │
    │                │                │ fires ContextChanged│
    │                │                │                  │
    │                │                │ SafetyGuard:     │
    │                │                │ new MRN ≠ session│
    │                │                │ MRN → MISMATCH   │
    │                │                │                  │
    │                │                │ IMMEDIATE ACTIONS│
    │                │                │ (in order):      │
    │                │                │                  │
    │                │                │ 1. Stop audio    │
    │                │                │    buffer flush  │
    │                │                │    (do NOT send  │
    │                │                │    ORU for       │
    │                │                │    incomplete    │
    │                │                │    dictation)    │
    │                │                │                  │
    │                │                │ 2. Set session   │
    │                │                │    state=SAFETY_ │
    │                │                │    HOLD          │
    │                │                │                  │
    │                │                │ 3. AuditLog:     │
    │                │                │ CONTEXT_MISMATCH │
    │                │                │ oldMRN=xxxx1234  │
    │                │                │ newMRN=xxxx9999  │
    │                │                │                  │
    │                │                │──────────────────►
    │                │                │ WS push:         │
    │                │                │ ErrorNotification│
    │                │                │ {                │
    │                │                │  code:"CONTEXT_  │
    │                │                │  CHANGED",       │
    │                │                │  severity:"HARD_  │
    │                │                │  BLOCK",         │
    │                │                │  message:"Patient│
    │                │                │  context has     │
    │                │                │  changed on the  │
    │                │                │  workstation.    │
    │                │                │  Dictation stopped│
    │                │                │  Verify correct  │
    │                │                │  patient before  │
    │                │                │  continuing.",   │
    │                │                │  requiresAck:true│
    │                │                │ }                │
    │◄─────────────────────────────────────────────────  │
    │ [RED BANNER:   │                │                  │
    │  STOP — Patient│                │                  │
    │  context changed│               │                  │
    │  Dictation halted│              │                  │
    │  Tap to dismiss │               │                  │
    │  (requires     │                │                  │
    │  explicit ack)]│                │                  │
    │                │                │                  │
    │ Rad realizes   │                │                  │
    │ the mistake,   │                │                  │
    │ goes back to   │                │                  │
    │ correct exam   │                │                  │
    │───────────────►│                │                  │
    │                │ CCOW publishes │                  │
    │                │ MRN=MRN123     │                  │
    │                │ ACC=ACC456     │                  │
    │                │────────────────►                  │
    │                │                │ SafetyGuard:     │
    │                │                │ context matches  │
    │                │                │ session again    │
    │                │                │ state=SAFETY_    │
    │                │                │ HOLD_RESOLVED    │
    │                │                │ AuditLog:        │
    │                │                │ CONTEXT_RESTORED │
    │                │                │──────────────────►
    │                │                │ StatusUpdate{    │
    │                │                │  status:"SAFETY_ │
    │                │                │  HOLD_RESOLVED", │
    │                │                │  canResume: true │
    │                │                │ }                │
    │◄─────────────────────────────────────────────────  │
    │ [Banner clears,│                │                  │
    │  prompt:       │                │                  │
    │  "Resume       │                │                  │
    │  dictation?"]  │                │                  │

── If context does NOT come back (different patient confirmed) ──

    │                │                │                  │
    │                │                │ SafetyGuard:     │
    │                │                │ after 30s or     │
    │                │                │ explicit discard:│
    │                │                │ discard audio    │
    │                │                │ buffer           │
    │                │                │ state=DISCARDED  │
    │                │                │ AuditLog:        │
    │                │                │ DICTATION_DISCARDED│
    │                │                │──────────────────►
    │                │                │ StatusUpdate{    │
    │                │                │ status:"DISCARDED"│
    │                │                │ }                │
    │◄─────────────────────────────────────────────────  │
    │ [Phone shows:  │                │                  │
    │  Dictation     │                │                  │
    │  discarded.    │                │                  │
    │  Re-pair for   │                │                  │
    │  new patient]  │                │                  │
```
