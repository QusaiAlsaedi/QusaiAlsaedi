# Mobile Dictation Agent — Windows Agent Design

## 4. Agent Design

### 4.1 Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| Runtime | .NET 8 Windows | Native Windows Service, DPAPI, COM interop, long-term support |
| HTTP/WS | ASP.NET Core / Kestrel embedded | In-process, no IIS required |
| Database | SQLite via Microsoft.Data.Sqlite | Device registry, session log — single file, encrypted |
| Queue store | Encrypted flat files | Survives process crash, no DB dependency |
| COM interop | CCOW COM via `System.Runtime.InteropServices` | Standard CCOW integration |
| HL7 MLLP | Custom TCP + TcpClient | Lightweight, no heavy HL7 library needed |
| Logging | Serilog (JSON sink) | Structured, PHI masking via destructuring policy |
| Tray icon | WinForms `NotifyIcon` | System tray QR popup |

---

### 4.2 Module Designs

#### 4.2.1 ContextProvider

```csharp
// Interfaces
public interface IContextProvider
{
    event EventHandler<ClinicalContextChangedArgs> ContextChanged;
    ClinicalContext? CurrentContext { get; }
    void Start();
    void Stop();
}

// Implementation selects strategy based on config
public class CompositeContextProvider : IContextProvider
{
    // Priority: CCOW (live) > LaunchArgs (one-shot) > none
    private readonly CcowContextProvider _ccow;
    private readonly LaunchArgsContextProvider _launchArgs;
}

public class LaunchArgsContextProvider : IContextProvider
{
    // Parses: /mrn=X /acc=Y /pt=LAST^FIRST /dob=YYYYMMDD /sex=M
    //         /mod=CT /proc=TEXT /dt=YYYYMMDDHHMMSS /site=X /prov=X
    // Also reads from named pipe: \\.\pipe\MDA_CONTEXT (for non-CLI launch)
    // Also reads from registry: HKCU\Software\MDA\PendingContext (one-shot key)
}

public class CcowContextProvider : IContextProvider
{
    // COM: IContextManager2 from ccow.dll (Hyland/Oacis CCOW)
    // Subscribe to ContextChangePending + ContextChanged events
    // Map CCOW items:
    //   Patient.Id.MRN       → MRN
    //   Patient.Name         → PatientName
    //   Patient.DateOfBirth  → DOB
    //   Patient.Sex          → Sex
    //   Order.AccessionNumber → Accession
    //   (CCOW item names are configurable in appsettings.json)
}

public class ClinicalContext
{
    public string Mrn         { get; init; } = "";
    public string Accession   { get; init; } = "";
    public string PatientName { get; init; } = "";
    public DateOnly Dob       { get; init; }
    public string Sex         { get; init; } = "";
    public string Modality    { get; init; } = "";
    public string Procedure   { get; init; } = "";
    public DateTime OrderDateTime    { get; init; }
    public string PerformingSite     { get; init; } = "";
    public string OrderingProvider   { get; init; } = "";
    public DateTime ReceivedAt       { get; init; } = DateTime.UtcNow;
}
```

---

#### 4.2.2 SessionManager

```csharp
public enum SessionState
{
    Waiting, Paired, Reading, Dictating, Draft,
    SecondReadPending, Final, SafetyHold,
    Discarded, Closed, Error
}

public class Session
{
    public Guid SessionId       { get; init; } = Guid.NewGuid();
    public string WindowsUser   { get; init; } = "";
    public string DeviceId      { get; set; } = "";
    public ClinicalContext OriginalContext { get; init; } = null!;
    public ClinicalContext CurrentContext  { get; set; } = null!;
    public SessionState State   { get; set; } = SessionState.Waiting;
    public Guid? CurrentDictationId { get; set; }
    public DateTime CreatedAt   { get; init; } = DateTime.UtcNow;
    public DateTime? PairedAt   { get; set; }
    public DateTime? ClosedAt   { get; set; }
}

public class SessionManager : ISessionManager
{
    // Only one active session per Windows user at a time
    // Thread-safe (ConcurrentDictionary keyed by WindowsUser)
    public Session CreateSession(ClinicalContext ctx, string windowsUser);
    public Session? GetActiveSession(string windowsUser);
    public void TransitionState(Guid sessionId, SessionState newState);
    public void CloseSession(Guid sessionId, string reason);
}
```

---

#### 4.2.3 SafetyGuard

```csharp
public class SafetyGuard : ISafetyGuard
{
    public ContextMismatchResult CheckContextChange(
        ClinicalContext sessionCtx,
        ClinicalContext newCtx)
    {
        var mismatches = new List<string>();
        if (sessionCtx.Mrn != newCtx.Mrn)           mismatches.Add("mrn");
        if (sessionCtx.Accession != newCtx.Accession) mismatches.Add("accession");
        return new ContextMismatchResult
        {
            HasMismatch = mismatches.Count > 0,
            MismatchFields = mismatches,
            // Only MRN or accession mismatch triggers SAFETY_HOLD
            TriggersSafetyHold = mismatches.Any(m => m is "mrn" or "accession")
        };
    }

    public ApproveValidationResult ValidateApprove(Session session)
    {
        var errors = new List<string>();

        if (string.IsNullOrEmpty(session.OriginalContext.Mrn))
            errors.Add("MRN missing");

        if (string.IsNullOrEmpty(session.OriginalContext.Accession))
            errors.Add("Accession missing");

        if (session.CurrentContext.Mrn != session.OriginalContext.Mrn)
            errors.Add("MRN changed since session start");

        if (session.CurrentContext.Accession != session.OriginalContext.Accession)
            errors.Add("Accession changed since session start");

        if (session.State is not (SessionState.Draft or SessionState.SecondReadPending))
            errors.Add($"Invalid state for approve: {session.State}");

        return new ApproveValidationResult
        {
            IsValid = errors.Count == 0,
            Errors = errors,
            RequiresOverride = errors.Any(e => e.Contains("changed"))
        };
    }
}
```

---

#### 4.2.4 DeviceManager

```csharp
public class DeviceManager : IDeviceManager
{
    // PairToken = HMAC-SHA256(sessionId + windowsUser + timestamp, machineSecret)
    // Stored as base64url, 60s TTL, single-use (nonce tracking)

    public string GeneratePairToken(Guid sessionId, string windowsUser);

    public PairTokenValidation ValidatePairToken(
        string token, Guid sessionId, string windowsUser);

    public string IssueSessionJwt(Guid sessionId, string deviceId, string windowsUser);

    public JwtValidation ValidateSessionJwt(string jwt, Guid sessionId);

    // Device registry stored in encrypted SQLite
    // Schema: (deviceId, windowsUser, pairedAt, lastSeen, revokedAt, model, os)
    public void RegisterDevice(string deviceId, string windowsUser, string model, string os);
    public void RevokeDevice(string deviceId);
    public bool IsDeviceRevoked(string deviceId);

    // QR payload structure:
    // {
    //   "v": 1,
    //   "ep": "wss://192.168.1.10:8443/mda/v1",
    //   "pt": "<pairToken>",
    //   "fp": "<tlsCertSha256Fingerprint>",
    //   "exp": 1740000060
    // }
    public string GenerateQrPayload(Guid sessionId, string windowsUser, string agentIp);
}
```

---

#### 4.2.5 Hl7OruBuilder

```csharp
public class Hl7OruBuilder : IHl7OruBuilder
{
    private readonly Hl7TemplateConfig _config;

    // Loads template from hl7_template.json
    // Performs token substitution
    public string BuildDraftOru(Session session, string reportText);
    public string BuildFinalOru(Session session, string reportText, string approverName);

    // Template tokens:
    // {MSG_CTRL_ID}     - GUID (unique per message)
    // {TIMESTAMP}       - HL7 DTM format: YYYYMMDDHHMMSS
    // {MRN}             - from session context
    // {ACC}             - from session context
    // {PATIENT_NAME}    - HL7 format: LAST^FIRST^MI
    // {DOB}             - YYYYMMDD
    // {SEX}             - M/F/O/U
    // {MODALITY}        - CT/MR/US etc.
    // {PROCEDURE}       - procedure description
    // {ORDER_DATETIME}  - HL7 DTM
    // {PERFORMING_SITE} - site code
    // {ORDERING_PROVIDER}
    // {STATUS}          - P (draft) or F (final)
    // {REPORT_TEXT}     - report body (escaped for HL7: | → \F\, ^ → \S\ etc.)
    // {APPROVER_NAME}   - for final
    // {SENDING_APP}     - from config
    // {SENDING_FACILITY}- from config
    // {RECEIVING_APP}   - from config
    // {RECEIVING_FACILITY}- from config
    // {DICTATION_ID}    - session dictation UUID
}
```

---

#### 4.2.6 Hl7Sender

```csharp
public class Hl7Sender : IHl7Sender
{
    // Transport selected from config: MLLP or FILE_DROP

    // MLLP send:
    // Frame: 0x0B + HL7 message bytes + 0x1C 0x0D
    // Read ACK response (parse MSA-1: AA/AE/AR)
    // TLS: SslStream over TcpClient

    // File drop:
    // Write to config folder as {dictationId}_{timestamp}.hl7
    // Rename from .tmp to .hl7 atomically (move)

    // Retry policy:
    // Attempt 1: immediate
    // Attempt 2: +5s
    // Attempt 3: +30s
    // Attempt 4: +120s
    // After 4 failures: move to dead-letter queue

    public Task<SendResult> SendAsync(OutboundMessage message, CancellationToken ct);
    public Task<SendResult> RetryAsync(OutboundMessage message, int attempt, CancellationToken ct);
}

public class SendResult
{
    public bool Success       { get; init; }
    public string AckCode     { get; init; } = ""; // AA / AE / AR
    public string AckMessage  { get; init; } = "";
    public int    Attempt     { get; init; }
    public Exception? Error   { get; init; }
}
```

---

#### 4.2.7 OutboundQueue

```csharp
// Queue directory structure:
// %ProgramData%\MDA\queue\
//   pending\    ← messages waiting to send
//   inflight\   ← messages currently being attempted
//   deadletter\ ← failed after all retries
//   sent\       ← successfully ACKed (retained 30 days, configurable)

public class OutboundQueue : IOutboundQueue
{
    // Each file = AES-256-GCM encrypted JSON
    // File name = {priority}_{timestamp}_{msgId}.mda
    // Priority prefix: 1=FINAL, 2=DRAFT, 3=retry

    public void Enqueue(OutboundMessage message);
    public OutboundMessage? Dequeue();
    public void MarkSent(string messageId);
    public void MoveToDeadLetter(string messageId, string reason);
    public void RequeueDeadLetters(); // manual admin action

    // On Agent start: move any inflight/ files back to pending/
    // (crash recovery)
}

public class OutboundMessage
{
    public string MessageId       { get; init; } = Guid.NewGuid().ToString();
    public string SessionId       { get; init; } = "";
    public string DictationId     { get; init; } = "";
    public string Hl7Content      { get; init; } = ""; // raw HL7 (plaintext in memory)
    public OruStatus Status       { get; init; }      // DRAFT or FINAL
    public int    AttemptCount    { get; set; }
    public DateTime CreatedAt     { get; init; } = DateTime.UtcNow;
    public DateTime? NextRetryAt  { get; set; }
    public string? LastError      { get; set; }
}
```

---

#### 4.2.8 AuditLog

```csharp
// PHI masking: destructuring policy on Serilog
// MRN: keep first 0 chars, show "MRN-xxxx" + last 4
// PatientName: always "[REDACTED]"
// DOB: only year shown "DOB-YYYY"
// AccessionNumber: show first 3 + "xxxx" + last 2

public class AuditLog : IAuditLog
{
    // JSON-lines format, one entry per line
    // Written to: %ProgramData%\MDA\audit\audit_{date}.jsonl
    // Rotated daily, retained 7 years (configurable)
    // File permissions: SYSTEM + Administrators only

    public void Log(AuditEvent evt);
}

public class AuditEvent
{
    public string   EventId      { get; init; } = Guid.NewGuid().ToString();
    public DateTime Timestamp    { get; init; } = DateTime.UtcNow;
    public string   EventType    { get; init; } = ""; // PAIR_OK, READ_STARTED, etc.
    public string   SessionId    { get; init; } = "";
    public string   WindowsUser  { get; init; } = "";
    public string   DeviceId     { get; init; } = "";
    public string   MaskedMrn    { get; init; } = "";  // "MRN-xxxx1234"
    public string   MaskedAccession { get; init; } = ""; // "ACCxxxx56"
    public string?  Details      { get; init; }   // non-PHI detail string
    public string?  OverrideReason { get; init; } // for controlled overrides
}

// Event types:
// AGENT_STARTED, AGENT_STOPPED
// CONTEXT_RECEIVED, CONTEXT_CHANGED, CONTEXT_MISMATCH, CONTEXT_RESTORED
// PAIR_OK, PAIR_FAILED, DEVICE_REVOKED
// SESSION_CREATED, SESSION_CLOSED
// READ_STARTED, CO_READ_STARTED
// DICTATION_STARTED, DICTATION_STOPPED, DICTATION_DISCARDED
// APPROVE_REQUESTED, APPROVE_BLOCKED, APPROVE_OVERRIDE, APPROVE_OK
// ORU_BUILT, ORU_SENT, ORU_ACK, ORU_NAK, ORU_DEAD_LETTER
// SAFETY_HOLD, SAFETY_HOLD_RESOLVED
```

---

#### 4.2.9 Encryption

```csharp
public class EncryptionService : IEncryptionService
{
    // Key derivation: DPAPI (machine scope) wraps a 256-bit master key
    // stored in %ProgramData%\MDA\.key (DPAPI-protected blob)
    // Only the same Windows machine can decrypt

    // Queue file encryption:
    // AES-256-GCM, random 96-bit nonce per file
    // File layout: [4 bytes nonce_len][nonce][ciphertext][16 bytes auth_tag]

    public byte[] Encrypt(byte[] plaintext);
    public byte[] Decrypt(byte[] ciphertext);
    public string EncryptString(string plaintext);
    public string DecryptString(string ciphertext);
}
```

---

### 4.3 Configuration Schema

**`appsettings.json`:**
```jsonc
{
  "Agent": {
    "ListenAddress": "0.0.0.0",
    "ListenPort": 8443,
    "TlsCertPath": "%ProgramData%\\MDA\\certs\\agent.pfx",
    "TlsCertPassword": "",          // use DPAPI-encrypted field in practice
    "PairTokenTtlSeconds": 60,
    "SessionJwtTtlHours": 8,
    "MaxDevicesPerUser": 2,
    "HeartbeatIntervalSeconds": 15,
    "HeartbeatTimeoutSeconds": 10
  },

  "Context": {
    "Source": "Composite",          // "LaunchArgs" | "CCOW" | "Composite"
    "CcowEnabled": true,
    "CcowProgId": "CCOWContextManager.ContextManager",
    "CcowItems": {
      "Mrn":             "Patient.Id.MRN",
      "PatientName":     "Patient.Name",
      "Dob":             "Patient.DateOfBirth",
      "Sex":             "Patient.Sex",
      "Accession":       "Order.AccessionNumber",
      "Modality":        "Order.Placer.Modality",
      "Procedure":       "Order.Placer.ProcedureText",
      "OrderDateTime":   "Order.Placer.DateTime",
      "PerformingSite":  "Order.Placer.PerformingSite",
      "OrderingProvider":"Order.Placer.OrderingProvider"
    },
    "LaunchArgPrefix": "/",          // or "-" depending on Centricity version
    "NamedPipe": "\\\\.\\pipe\\MDA_CONTEXT"
  },

  "Hl7": {
    "Transport": "MLLP",             // "MLLP" | "FILE_DROP"
    "Mllp": {
      "Host": "hl7-engine.hospital.local",
      "Port": 2575,
      "UseTls": true,
      "ConnectTimeoutSeconds": 10,
      "ReadTimeoutSeconds": 30
    },
    "FileDrop": {
      "Folder": "C:\\MDA\\hl7-out",
      "FileExtension": ".hl7",
      "TempExtension": ".tmp"
    },
    "Retry": {
      "MaxAttempts": 4,
      "BackoffSeconds": [5, 30, 120]
    },
    "SendingApplication":  "MDA",
    "SendingFacility":     "RADIOLOGY",
    "ReceivingApplication": "RIS",
    "ReceivingFacility":    "HOSPITAL"
  },

  "Storage": {
    "BasePath": "%ProgramData%\\MDA",
    "QueueFolder":       "queue",
    "AuditFolder":       "audit",
    "AudioFolder":       "audio",
    "DeviceDbPath":      "devices.db",
    "AuditRetentionDays": 2555,      // 7 years
    "SentRetentionDays": 30,
    "AudioRetentionDays": 1          // audio files deleted after 24h
  },

  "Safety": {
    "ContextMismatchBlocksApprove": true,
    "AllowControlledOverride": true,
    "SafetyHoldAutoDiscardSeconds": 300  // 5 min of SAFETY_HOLD → auto-discard
  },

  "FootPedal": {
    "Enabled": false,
    "StartKey": "F13",
    "StopKey": "F14",
    "ApproveKey": "F15"
  },

  "Serilog": {
    "MinimumLevel": "Information",
    "WriteTo": [
      { "Name": "File", "Args": {
        "path": "%ProgramData%\\MDA\\logs\\agent-.log",
        "rollingInterval": "Day",
        "retainedFileCountLimit": 30,
        "outputTemplate": "{Timestamp:yyyy-MM-dd HH:mm:ss.fff} [{Level:u3}] {Message:lj}{NewLine}{Exception}"
      }}
    ]
  }
}
```

---

### 4.4 Storage Strategy

```
%ProgramData%\MDA\
├── .key                    ← DPAPI-protected master key blob (SYSTEM only)
├── certs\
│   └── agent.pfx           ← TLS certificate + private key
├── queue\
│   ├── pending\            ← {priority}_{ts}_{msgId}.mda (encrypted)
│   ├── inflight\           ← moved here during send attempt
│   ├── deadletter\         ← failed messages (encrypted, for manual review)
│   └── sent\               ← ACKed messages (encrypted, auto-purged after 30d)
├── audio\
│   └── {sessionId}\
│       └── {dictationId}.opus  ← raw audio (encrypted, auto-purged after 24h)
├── audit\
│   └── audit_{yyyy-MM-dd}.jsonl  ← masked PHI only
├── logs\
│   └── agent-{date}.log    ← no PHI
└── devices.db              ← SQLite (encrypted via SQLCipher or file-level encryption)
```

**Queue file format (encrypted blob, decrypted payload):**
```json
{
  "messageId":    "uuid",
  "sessionId":    "uuid",
  "dictationId":  "uuid",
  "status":       "FINAL",
  "hl7Content":   "MSH|...\rPID|...\r...",
  "attemptCount": 0,
  "createdAt":    "ISO8601",
  "nextRetryAt":  null,
  "lastError":    null
}
```

---

### 4.5 Windows Service Lifecycle

```
On Install:
  - Create service account (LocalSystem or dedicated svc account)
  - Generate TLS cert (self-signed, 2-year validity, store fingerprint)
  - Initialize DPAPI master key
  - Open Windows Firewall rule for TCP 8443 inbound

On Start:
  - Load config
  - Initialize encryption service
  - Recover inflight queue messages → pending
  - Start Kestrel HTTPS server
  - Start ContextProvider (CCOW subscription + named pipe listener)
  - Start OutboundQueue processor (background service)
  - Show system tray icon (green = idle, yellow = paired, red = error)

On Context Received:
  - Create/update session
  - Generate QR token
  - Show QR popup in tray

On Stop:
  - Graceful shutdown: flush audio buffers, save session state
  - Close WebSocket connections (notify phones)
  - Stop Kestrel
  - Unsubscribe CCOW
```
