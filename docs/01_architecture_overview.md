# Mobile Dictation Agent — Architecture Overview

## 1. Architecture Overview

### 1.1 System Components and Responsibilities

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         HOSPITAL NETWORK (TLS everywhere)                   │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                  RIS WORKSTATION (Windows 10/11)                      │  │
│  │                                                                       │  │
│  │  ┌─────────────────┐    launch args / CCOW    ┌──────────────────┐  │  │
│  │  │  GE Centricity   │ ─────────────────────►  │  Windows Agent   │  │  │
│  │  │      RIS         │                          │  (MDA-Agent.exe) │  │  │
│  │  └─────────────────┘                          │                  │  │  │
│  │                                               │  ContextProvider  │  │  │
│  │  ┌─────────────────┐                          │  SessionManager   │  │  │
│  │  │  CCOW Context   │ ──────────────────────►  │  SafetyGuard      │  │  │
│  │  │  Manager (COM)  │                          │  Hl7OruBuilder    │  │  │
│  │  └─────────────────┘                          │  Hl7Sender        │  │  │
│  │                                               │  OutboundQueue    │  │  │
│  │  ┌─────────────────┐                          │  AuditLog         │  │  │
│  │  │  USB Foot Pedal │ ──── HID hotkeys ──────► │  DeviceManager    │  │  │
│  │  │  (optional)     │                          │  Encryption       │  │  │
│  │  └─────────────────┘                          └──────┬───────────┘  │  │
│  │                                                       │              │  │
│  │                                               HTTPS WebSocket        │  │
│  │                                               (localhost or LAN)     │  │
│  └──────────────────────────────────────────────────────┼───────────────┘  │
│                                                          │                  │
│           ┌──────────────────────────────────────────────┘                  │
│           │                                                                  │
│           ▼                                                                  │
│  ┌─────────────────────┐                                                    │
│  │   MOBILE PHONE       │                                                   │
│  │  (iOS / Android)     │                                                   │
│  │                      │                                                   │
│  │  ┌────────────────┐  │                                                   │
│  │  │  Patient Header │  │                                                   │
│  │  │  Case Details   │  │                                                   │
│  │  │  Workflow Btns  │  │                                                   │
│  │  │  Status Banner  │  │                                                   │
│  │  └────────────────┘  │                                                   │
│  │  ┌────────────────┐  │                                                   │
│  │  │  Audio Capture  │  │                                                   │
│  │  │  (PCM/Opus)     │  │                                                   │
│  │  └────────────────┘  │                                                   │
│  └─────────────────────┘                                                    │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                   HL7 INFRASTRUCTURE                                   │  │
│  │                                                                       │  │
│  │    Agent  ──MLLP (2575/TLS)──►  Interface Engine  ──►  LIS/RIS       │  │
│  │    Agent  ──file-drop──────────►  Folder watch     ──►  LIS/RIS       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### 1.2 Component Responsibilities

#### A. GE Centricity RIS (existing — no modification)
- Launches dictation via command-line invocation or CCOW context publish
- Provides launch arguments: `MRN`, `AccessionNumber`, `PatientName`, `DOB`, `Sex`, `Modality`, `Procedure`, `OrderDateTime`, `PerformingSite`, `OrderingProvider`
- Publishes CCOW context items when the active patient/exam changes

#### B. Windows Agent (`MDA-Agent.exe`) — .NET 8, Windows Service + System Tray
The central orchestrator. All PHI flows through here and nowhere else on the server side.

| Module | Responsibility |
|---|---|
| **ContextProvider** | Receives launch args (CLI/registry/named pipe) and polls CCOW COM via `CCOWContextManager`. Normalizes into `ClinicalContext` struct. |
| **SessionManager** | Manages the lifecycle of a radiologist session: created → paired → active → closed. Holds `SessionId`, `WindowsUser`, `DeviceId`, `ClinicalContext`. |
| **SafetyGuard** | Compares incoming CCOW/launch-args context to the active session context. Triggers `ContextChangedEvent` if MRN or Accession differs. Blocks Approve if not confirmed. |
| **DeviceManager** | Issues QR pairing tokens (TOTP-style, 60s), authenticates devices, maintains device registry (encrypted SQLite), supports revocation. |
| **Hl7OruBuilder** | Template-based ORU generator. Loads segment templates from `hl7_template.json`. Fills `{MRN}`, `{ACC}`, `{REPORT_TEXT}`, `{STATUS}`, `{TIMESTAMP}` etc. |
| **Hl7Sender** | Sends HL7 via MLLP (TLS) or file-drop. Handles ACK/NAK, retry with exponential backoff (3 attempts, 5/30/120s), dead-letter queue. |
| **OutboundQueue** | Durable, encrypted-at-rest queue (files in `%ProgramData%\MDA\queue\`). Each message = encrypted JSON file. Survives Agent restart. |
| **AuditLog** | Append-only structured log (JSON lines). PHI is masked: MRN shown as `MRN-xxxx[last4]`, name as `[REDACTED]`. Written to `%ProgramData%\MDA\audit\`. |
| **Encryption** | AES-256-GCM for queue files and device registry. Key derived from Windows DPAPI (machine scope) so only the same machine can decrypt. |
| **WebSocketServer** | Embedded Kestrel HTTPS server on `localhost:8443` (or LAN IP). Serves the phone-Agent API. |

#### C. Mobile App (`MDA-Phone`) — React Native (iOS + Android)
- **PairingScreen**: Shows QR scanner, captures pairing token
- **SessionScreen**: Header (patient + case), workflow buttons, status banner, warning overlays
- **DictationController**: Manages mic lifecycle, streams audio chunks to Agent
- **EphemeralStore**: All PHI in memory only, cleared on session end or app background >2 min
- **NetworkClient**: WebSocket + HTTPS to Agent, with reconnect and heartbeat

#### D. HL7 Interface / Downstream (existing — no modification)
- Receives ORU messages via MLLP or file-drop
- Returns ACK/NAK to Agent

---

### 1.3 USB Approach — Analysis and Recommendation

#### Option A: Wi-Fi / LAN (purely wireless)
- **Pros**: Zero physical setup, works anywhere in department, seamless roaming, QR pairing only
- **Cons**: Depends on Wi-Fi quality; hospital Wi-Fi may have isolation policies between device VLANs
- **Recommended for**: Most deployments

#### Option B: Android USB Tethering
- **Pros**: Guaranteed private link (192.168.x.x/30), bypasses Wi-Fi VLAN issues, no radio interference
- **Cons**: Android only, USB cable required at desk, must enable tethering manually, Windows installs RNDIS driver
- **Recommended for**: High-security environments requiring no Wi-Fi

#### Option C: iPhone Personal Hotspot via USB (CarPlay / Lightning/USB-C)
- **Pros**: Private link via Apple's USB Ethernet adapter mode, reliable, fast
- **Cons**: iOS-only, requires trust confirmation on each new Mac/Windows pairing, USB cable at desk

#### Option D: USB Foot Pedal (HID) + Wi-Fi Phone
- **Pros**: Ergonomically familiar to radiologists, foot pedal sends Start/Stop/Approve hotkeys to Agent, phone stays wireless
- **Cons**: Additional hardware (foot pedal ~$50), two devices to manage
- **Recommended for**: Radiologists migrating from traditional dictaphone workflow

#### **Recommendation: Option A (Wi-Fi/LAN) as default with Option D (USB foot pedal) as optional add-on**
- All Agent and phone code is identical regardless of transport
- Foot pedal registers as HID keyboard; Agent intercepts hotkeys (`F13`=Start, `F14`=Stop, `F15`=Approve)
- If a site requires "no Wi-Fi for PHI", fall back to Option B (Android USB tethering) — the Agent automatically detects the tethered IP

---

### 1.4 Network Topology

```
[Phone] ──TLS/WSS──► [Agent :8443] (localhost or hospital LAN)
[Agent] ──MLLP/TLS──► [HL7 Interface Engine :2575]
[Agent] ──file write──► [\\server\hl7-drop\] (UNC share, alternative)
[CCOW]  ──COM/DCOM──► [Agent] (same machine, in-process COM interop)
```

**Ports opened on workstation firewall (inbound):**
- `TCP 8443` — Agent WebSocket/HTTPS (from phone IP only if possible)
- All outbound only for MLLP and file-drop
