# Mobile Dictation Agent — MVP Build Plan

## 7. MVP Build Plan

### Overview

| Phase | Duration | Goal | HL7 Transport | Context Source | Audio |
|---|---|---|---|---|---|
| **Phase 1** | 6 weeks | Prove the workflow end-to-end | File-drop | Launch args only | Manual text / .wav file |
| **Phase 2** | 6 weeks | Production-grade integration | MLLP + TLS | CCOW + Launch args | Streaming Opus |
| **Phase 3** | 8 weeks | Full feature set | MLLP + file-drop | CCOW + Launch args | STT + device mgmt |

---

### Phase 1 — Core Workflow (6 Weeks)

**Goal:** Working end-to-end flow on a single workstation. Radiologist can scan QR, see patient info, tap Read/Approve, and have a DRAFT/FINAL HL7 written to a folder. No streaming audio — uses a manually entered transcription text box on the phone.

#### Week 1–2: Agent Foundation

- [ ] `.NET 8` Windows Service project scaffold
- [ ] `LaunchArgsContextProvider` — parse `/mrn=`, `/acc=`, `/pt=`, `/dob=`, `/sex=`, `/mod=`, `/proc=`, `/dt=`, `/site=`, `/prov=`
- [ ] `ClinicalContext` model + validation
- [ ] `SessionManager` — create/get/transition session state
- [ ] `EncryptionService` — DPAPI key init + AES-256-GCM encrypt/decrypt
- [ ] `DeviceManager` — `GeneratePairToken`, `ValidatePairToken`, `IssueSessionJwt`, `ValidateSessionJwt`
- [ ] Kestrel embedded HTTPS on port 8443 with self-signed cert
- [ ] `POST /mda/v1/pair` endpoint
- [ ] WebSocket endpoint at `wss://localhost:8443/mda/v1/ws`
- [ ] System tray icon with QR display (WinForms `PictureBox` + QRCoder NuGet)
- [ ] `AuditLog` — JSON-lines with PHI masking

**Deliverable:** Agent launches, shows QR, phone pairs, sees patient context.

#### Week 3–4: Workflow + File-Drop HL7

- [ ] `SafetyGuard` — `CheckContextChange`, `ValidateApprove`
- [ ] WebSocket message routing for `WORKFLOW_ACTION`, `STATUS_UPDATE`, `ERROR_NOTIFICATION`
- [ ] `Hl7OruBuilder` — template loading from `hl7_template.json`, token substitution, HL7 escaping
- [ ] `OutboundQueue` — file-based encrypted queue (pending/inflight/sent/deadletter)
- [ ] `Hl7Sender` — file-drop transport (write `.tmp`, rename to `.hl7`)
- [ ] Retry logic (4 attempts, configurable backoff)
- [ ] Context change detection via CCOW polling (stub for Phase 1: manual test button in tray)
- [ ] `CONTEXT_CHANGED` event → `SAFETY_HOLD` → push `ERROR_NOTIFICATION` to phone

**Deliverable:** Full Read → Draft → Approve → Final → HL7 file written.

#### Week 5–6: Mobile App (Phase 1)

- [ ] React Native project scaffold (iOS + Android)
- [ ] `PairingScreen` — camera-based QR scanner, parse QR payload, call `/pair`
- [ ] TLS cert fingerprint pinning via `react-native-ssl-pinning`
- [ ] `SessionScreen` — patient header, case details, workflow buttons
- [ ] In-memory store (Zustand) — `ClinicalContext`, `SessionState`
- [ ] WebSocket connection manager — connect, heartbeat, reconnect
- [ ] `WorkflowAction` send (READ / CO-READ / APPROVE)
- [ ] **Phase 1 audio**: Text input field on phone — radiologist types or pastes report text; sent as part of `StopDictation` message
- [ ] `STATUS_UPDATE` handler → update UI badge (DRAFT / FINAL)
- [ ] `ERROR_NOTIFICATION` handler → red warning banner (CONTEXT_CHANGED)
- [ ] `ContextWarningOverlay` — modal blocks all interaction on SAFETY_HOLD

**Deliverable:** Full working demo on a single workstation. Suitable for clinical workflow review and sign-off.

**Phase 1 Success Criteria:**
- Radiologist scans QR in <5 seconds
- Patient info appears correctly on phone from launch args
- Read → type report → Approve → HL7 file appears in drop folder within 3 seconds
- Context change (simulated) shows red banner on phone within 1 second
- Zero PHI in log files

---

### Phase 2 — Production Integration (6 Weeks)

**Goal:** Replace file-drop with MLLP, add real CCOW context, and stream audio from phone mic.

#### Week 7–8: CCOW Integration

- [ ] `CcowContextProvider` — COM interop with `IContextManager2`
- [ ] Map CCOW item names from `appsettings.json` → `ClinicalContext`
- [ ] `CompositeContextProvider` — priority: CCOW > LaunchArgs
- [ ] Real-time CCOW `ContextChanged` event subscription
- [ ] `SafetyGuard` wired to CCOW events — auto `SAFETY_HOLD` on mid-dictation mismatch
- [ ] Named pipe listener `\\.\pipe\MDA_CONTEXT` as alternative launch integration

**Deliverable:** Agent tracks live CCOW context changes, no polling needed.

#### Week 9–10: MLLP Sender + Retry + Dead-Letter

- [ ] `Hl7Sender` MLLP transport — `SslStream` over `TcpClient`, MLLP framing
- [ ] ACK parsing — `MSA-1`: AA/AE/AR/CA/CE/CR handling
- [ ] Retry scheduler (background `IHostedService`) — reads `nextRetryAt` from queue files
- [ ] Dead-letter alerting — Windows Event Log + tray balloon notification
- [ ] `OutboundQueue` crash recovery — move inflight → pending on startup
- [ ] Admin endpoint `POST /mda/v1/admin/requeue-deadletter` (localhost only, requires admin JWT)

**Deliverable:** Agent sends MLLP with TLS, handles NAK/retry, survives crashes.

#### Week 11–12: Audio Streaming

- [ ] Agent: audio buffer manager — receives binary WS frames, writes encrypted `.opus` to `audio/{sessionId}/`
- [ ] Agent: `StopDictation` handler — finalizes audio file, generates ORU with `{REPORT_TEXT}` from Phase 3 STT or empty placeholder
- [ ] Mobile app: `react-native-audio-record` integration with Opus encoding
- [ ] `AudioChunkSender` with sequence numbers, pending map, reconnect resend
- [ ] iOS audio session configuration (`PlayAndRecord`, `Measurement` mode)
- [ ] Android `VOICE_RECOGNITION` audio source
- [ ] Audio buffer overflow warning (notify phone if Agent buffer > 50 chunks)

**Deliverable:** Real mic audio streamed, stored encrypted on Agent. Report text is placeholder (full STT in Phase 3).

**Phase 2 Success Criteria:**
- CCOW context change triggers SAFETY_HOLD within 500ms
- MLLP ORU ACKed by interface engine in test environment
- NAK triggers retry; after 4 failures, dead-letter with tray alert
- Audio streaming works on both iOS and Android; no audio written to phone disk
- Agent survives restart with no message loss (queue recovered)

---

### Phase 3 — Full Feature Set (8 Weeks)

**Goal:** Add transcription, device management UI, monitoring, and multi-workstation support.

#### Week 13–14: Transcription Provider Interface

- [ ] `ITranscriptionProvider` interface:
  ```csharp
  public interface ITranscriptionProvider
  {
      Task<TranscriptionResult> TranscribeAsync(Stream audioStream, TranscriptionOptions opts);
  }
  ```
- [ ] `LocalWhisperProvider` — Whisper.NET (CPU, runs on workstation; Phase 3 default)
- [ ] `CloudTranscriptionProvider` — pluggable REST client (Nuance, AWS Transcribe Medical, Azure Speech)
- [ ] Transcription called after `StopDictation` → text injected into `{REPORT_TEXT}` in ORU
- [ ] Phone: display transcription result in read-only text area for review before Approve
- [ ] Phone: allow radiologist to edit transcription text (optional, site config)

#### Week 15–16: Device Management

- [ ] Admin web UI (served by Agent on port 8444, localhost only):
  - View paired devices
  - Revoke device (immediate session kill + blacklist)
  - View session history (masked PHI)
  - View dead-letter queue
  - Re-queue dead-letter messages
  - Download audit log (admin auth required)
- [ ] Device revocation pushed to phone via WebSocket `ERROR_NOTIFICATION{code: DEVICE_REVOKED}`
- [ ] `DeviceManager` — persistent SQLite device registry

#### Week 17–18: Multi-Workstation + Monitoring

- [ ] Agent: support multiple simultaneous sessions (one per Windows user on the same machine, or multi-machine deployment)
- [ ] Health endpoint `GET /mda/v1/health` → JSON (queue depth, last HL7 send time, CCOW status, no PHI)
- [ ] Prometheus metrics endpoint `/metrics` — for optional integration with hospital monitoring
- [ ] Windows Event Log structured entries for critical events (SAFETY_HOLD, DEAD_LETTER, DEVICE_REVOKED)
- [ ] Optional USB foot pedal integration:
  - Detect HID device (configurable VID/PID)
  - Map pedal buttons to hotkeys → `F13`/`F14`/`F15`
  - Agent intercepts via `RegisterHotKey` Win32 API

#### Week 19–20: Hardening + Documentation

- [ ] Penetration test (internal) — focus: WebSocket auth, MLLP injection, replay attacks
- [ ] Load test — 10 simultaneous sessions, 8h continuous (simulated)
- [ ] Installer (WiX Toolset) — silent install, firewall rule, cert generation
- [ ] Clinical validation pack — run 15 UAT scenarios with clinical team
- [ ] IT runbook — install, configure, troubleshoot, revoke, monitor
- [ ] HIPAA risk assessment update

**Phase 3 Success Criteria:**
- Transcription available and accurate (>90% WER target on radiology vocabulary)
- IT can revoke a device and it disconnects within 5 seconds
- Health endpoint returns useful data for NOC monitoring
- Installer completes in <5 minutes on a clean Windows 10 workstation
- All 20 unit tests + 15 UAT scenarios pass

---

### Dependency Map

```
Phase 1 ──► Phase 2
   │              │
   │              └──► Phase 3
   │                       │
   └──────────────────────►│
                            └──► Production Go-Live
```

### Technology Dependencies to Procure Before Phase 1

| Item | Notes |
|---|---|
| Code signing certificate | For Agent installer (required by some hospital IT policies) |
| TLS certificate | Self-signed is sufficient for Phase 1; hospital-issued for Phase 2+ |
| Test HL7 interface endpoint | MLLP listener for testing (e.g., Mirth Connect on dev server) |
| Test Centricity instance | Dev/test RIS for launch arg and CCOW testing |
| CCOW SDK / COM library | Must match the installed CCOW Context Manager version at the site |
| Apple Developer account | For iOS TestFlight distribution in Phase 1 testing |
| Google Play Internal Testing | For Android Phase 1 testing |
