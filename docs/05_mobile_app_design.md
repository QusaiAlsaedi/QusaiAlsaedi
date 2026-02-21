# Mobile Dictation Agent — Mobile App Design

## 5. Mobile App Design

### 5.1 Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| Framework | React Native 0.73+ | Single codebase for iOS + Android, community support |
| State | Zustand (lightweight) | Simple, no boilerplate, works well with ephemeral state |
| Navigation | React Navigation v6 | De-facto standard |
| WebSocket | `react-native-fast-ws` | Binary frame support (for audio chunks) |
| Audio capture | `react-native-audio-record` (custom fork with Opus) | Low-latency mic access, background audio session |
| QR scanner | `react-native-vision-camera` + `vision-camera-code-scanner` | Fast, handles low-light |
| TLS pinning | `react-native-ssl-pinning` | Pin TLS cert fingerprint from QR payload |
| Secure storage | iOS: Keychain; Android: EncryptedSharedPreferences | Session token storage only |
| Analytics/crash | Excluded intentionally | No PHI exfiltration risk |

---

### 5.2 UI Screens and Navigation

```
App Navigator
├── PairingScreen          (initial screen; shown when no active session)
│   └── QR Scanner overlay
├── SessionScreen          (active session; main screen)
│   ├── PatientHeader      (sticky top bar)
│   ├── CaseDetailsSection (collapsible)
│   ├── StatusBanner       (DRAFT / SECOND_READ_PENDING / FINAL / SAFETY_HOLD)
│   ├── WorkflowButtons    (READ / CO-READ / APPROVE)
│   ├── DictationControls  (START / STOP + timer + level meter)
│   └── ContextWarningOverlay (modal, blocks interaction when SAFETY_HOLD)
└── SettingsScreen         (accessible from tray only; for IT use)
    ├── Re-pair button
    ├── Revoke device button
    └── Version/cert info
```

---

### 5.3 Screen Wireframes

#### PairingScreen
```
┌─────────────────────────────────────────┐
│          Mobile Dictation Agent          │
│                                         │
│   ┌─────────────────────────────────┐   │
│   │                                 │   │
│   │        [CAMERA VIEWFINDER]      │   │
│   │                                 │   │
│   │    [ QR frame guide corners ]   │   │
│   │                                 │   │
│   │                                 │   │
│   └─────────────────────────────────┘   │
│                                         │
│  Scan the QR code shown on your         │
│  radiology workstation.                 │
│                                         │
│  The QR expires in 60 seconds.          │
│  Refresh it on the PC if needed.        │
│                                         │
│              [Cancel]                   │
└─────────────────────────────────────────┘
```

#### SessionScreen (normal state)
```
┌─────────────────────────────────────────┐
│ ■ DRAFT                     [Disconnect]│
├─────────────────────────────────────────┤
│ DOE, JOHN MICHAEL               M  74y  │  ← PatientHeader (always visible)
│ MRN: 1234567   DOB: 1950-01-01          │
├─────────────────────────────────────────┤
│ ACC: RAD-2025-456789            ▼       │  ← CaseDetailsSection (collapsible)
│ Modality:  CT                           │
│ Procedure: CT Chest with Contrast       │
│ Order:     2025-07-15 09:32             │
│ Site:      Main Radiology               │
│ Provider:  Dr. Smith, Jane              │
├─────────────────────────────────────────┤
│                                         │
│  ┌──────────┐ ┌──────────┐ ┌─────────┐ │
│  │          │ │          │ │         │ │
│  │   READ   │ │ CO-READ  │ │ APPROVE │ │
│  │          │ │          │ │         │ │
│  └──────────┘ └──────────┘ └─────────┘ │
│                                         │
├─────────────────────────────────────────┤
│                                         │
│  ████░░░░░░░░░░░░░░░░░░░░░░░ -24 dBFS  │  ← audio level meter
│                                         │
│         ┌─────────────────┐             │
│         │  ● STOP  01:23  │             │  ← dictation active
│         └─────────────────┘             │
│                                         │
│         ┌─────────────────┐             │
│         │  ◎ START        │             │  ← (shown when not dictating)
│         └─────────────────┘             │
│                                         │
└─────────────────────────────────────────┘
```

#### ContextWarningOverlay (SAFETY_HOLD)
```
┌─────────────────────────────────────────┐
│                                         │
│  ████████████████████████████████████  │
│  █                                  █  │
│  █   ⚠ PATIENT CONTEXT CHANGED ⚠   █  │
│  █                                  █  │
│  █  The workstation has switched to █  │
│  █  a different patient. Dictation  █  │
│  █  has been STOPPED automatically. █  │
│  █                                  █  │
│  █  DO NOT continue dictating until █  │
│  █  the correct patient is confirmed█  │
│  █  on the workstation.             █  │
│  █                                  █  │
│  █        [ I UNDERSTAND ]          █  │
│  █   (returns when context restored)█  │
│  █                                  █  │
│  ████████████████████████████████████  │
│                                         │
│  (All buttons disabled until resolved)  │
└─────────────────────────────────────────┘
```

---

### 5.4 State Machine (Phone-side)

```
DISCONNECTED
    │ QR scanned + pair success
    ▼
READY  ──────────────────────── tap READ ──────────────► READING
    │                                                       │
    │                                             tap START DICTATION
    │                                                       │
    │                                                       ▼
    │                                                  DICTATING
    │                                                       │
    │                                             tap STOP DICTATION
    │                                                       │
    │                                                       ▼
    │◄── AGENT pushes FINAL ──────────────── DRAFT ─── tap APPROVE
    │                                          │
    │                               tap CO-READ
    │                                          │
    │                                          ▼
    │                            SECOND_READ_PENDING
    │                                          │
    │                               tap START DICTATION (co-read)
    │                                   → DICTATING → DRAFT → APPROVE
    │
SAFETY_HOLD ←── Agent pushes CONTEXT_CHANGED (any state)
    │
    │  Agent pushes CONTEXT_RESTORED
    ▼
SAFETY_HOLD_RESOLVED → user taps resume → previous state
    │
    │  Agent pushes DISCARDED / user taps discard
    ▼
READY (re-pair for next exam)
```

---

### 5.5 Local Storage Rules (PHI Policy)

| Data | Storage | Cleared When |
|---|---|---|
| `SessionToken` (JWT) | iOS Keychain / Android EncryptedSharedPreferences | Session ends, app revoked, or re-pair |
| `AgentEndpoint` + `CertFingerprint` | Same secure store | Re-pair |
| `DeviceId` | Same secure store | Never (stable device identity) |
| `ClinicalContext` (MRN, name, etc.) | **In-memory only** (Zustand store) | Session end, app goes background >2 min, or device lock |
| Audio chunks | **Streaming only** — not written to disk | Immediately after WS send |
| `dictationId`, `sequenceNumber` | In-memory | Session end |

**Key rule: Zero PHI written to device disk at any time.** The only persistent items are the JWT (no PHI in claims) and the pairing endpoint. All PHI is ephemeral in memory.

**Background behavior:**
- If app goes to background while DICTATING: send `StopDictation{reason: APP_BACKGROUND}` to Agent, mic stops, PHI cleared from memory
- If app returns from background within 2 min: show "Re-pair to resume" prompt (session may still be alive on Agent side)
- If device locked: PHI cleared immediately

---

### 5.6 Audio Capture Strategy

#### Recommended: Opus streaming (real-time chunks)

```
┌────────────────────────────────────────────────────────────┐
│                    Audio Pipeline                           │
│                                                            │
│  Mic (16 kHz, mono) → PCM buffer (20ms frames)             │
│        → Opus encoder (16 kbps, complexity 5)              │
│        → 250ms Opus packet                                 │
│        → Binary WS frame (header + payload)                │
│        → Agent                                             │
└────────────────────────────────────────────────────────────┘

Bandwidth: ~2 KB/s (Opus 16kbps) — trivially within Wi-Fi + USB tethering
Latency:   250ms chunks → real-time streaming, suitable for live STT (Phase 3)
```

#### Fallback: PCM chunked (no Opus encoder available)
- 16-bit signed, 16 kHz, mono = 32 KB/s
- Still manageable on LAN, but too large for poor Wi-Fi

#### Retry handling for audio chunks

```typescript
class AudioChunkSender {
  private pendingChunks: Map<number, AudioChunk> = new Map();
  private maxPending = 50; // ~12.5s buffer

  async sendChunk(chunk: AudioChunk) {
    if (this.pendingChunks.size >= this.maxPending) {
      // Buffer overflow: pause mic, notify Agent, set warning
      this.emitWarning('AUDIO_BUFFER_OVERFLOW');
      return;
    }
    this.pendingChunks.set(chunk.sequenceNumber, chunk);
    await this.ws.sendBinary(this.serializeChunk(chunk));
  }

  onAck(seqNum: number) {
    this.pendingChunks.delete(seqNum);
  }

  onReconnect() {
    // Resend all pending chunks in order
    for (const chunk of this.pendingChunks.values()) {
      this.ws.sendBinary(this.serializeChunk(chunk));
    }
  }
}
```

#### iOS Audio Session Configuration
```typescript
// Must be set before recording starts
await AudioSession.setCategory('PlayAndRecord', {
  allowBluetooth: false,      // Force built-in mic for quality
  defaultToSpeaker: false,
  mixWithOthers: false,
});
await AudioSession.setMode('Measurement');  // Flat response, no AGC
await AudioSession.setActive(true);
```

#### Android Audio Configuration
```typescript
// React Native Audio Record config
AudioRecord.init({
  sampleRate: 16000,
  channels: 1,
  bitsPerSample: 16,
  audioSource: 6,  // VOICE_RECOGNITION — disables noise suppression
  wavFile: '',     // No file, streaming only
});
```

---

### 5.7 WebSocket Connection Management

```typescript
class AgentConnection {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private maxReconnects = 5;
  private heartbeatInterval: NodeJS.Timeout | null = null;

  connect(endpoint: string, certFingerprint: string) {
    this.ws = new WebSocket(endpoint, {
      headers: { Authorization: `Bearer ${sessionToken}` },
      sslPinning: { certs: [certFingerprint] }  // TLS pinning
    });

    this.ws.onclose = () => this.scheduleReconnect();
    this.ws.onmessage = (e) => this.handleMessage(e.data);
    this.startHeartbeat();
  }

  private scheduleReconnect() {
    if (this.reconnectAttempts >= this.maxReconnects) {
      this.emitFatal('MAX_RECONNECTS_EXCEEDED');
      return;
    }
    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 30000);
    setTimeout(() => {
      this.reconnectAttempts++;
      this.connect(this.endpoint, this.certFingerprint);
    }, delay);
  }

  private startHeartbeat() {
    this.heartbeatInterval = setInterval(() => {
      this.send({ type: 'HEARTBEAT', msgId: uuid(), ts: now(), payload: {} });
      // If no HEARTBEAT_ACK within 10s → trigger reconnect
    }, 15000);
  }
}
```
