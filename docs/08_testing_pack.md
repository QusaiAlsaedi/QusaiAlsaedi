# Mobile Dictation Agent — Testing Pack

## 8. Testing Pack

### 8.1 Unit Tests (Agent — 20 Tests)

Framework: **xUnit + FluentAssertions + NSubstitute** (C# .NET 8)

---

#### TEST-U-01: LaunchArgsContextProvider — Happy Path
```csharp
[Fact]
public void ParsesAllKnownArgumentsIntoContext()
{
    var args = new[] {
        "/mrn=1234567", "/acc=RAD-2025-456789",
        "/pt=DOE^JOHN^MICHAEL", "/dob=19500101", "/sex=M",
        "/mod=CT", "/proc=CT Chest with Contrast",
        "/dt=20250715093200", "/site=Main Radiology",
        "/prov=SMITH^JANE^DR"
    };
    var provider = new LaunchArgsContextProvider(args);
    var ctx = provider.CurrentContext;

    ctx.Mrn.Should().Be("1234567");
    ctx.Accession.Should().Be("RAD-2025-456789");
    ctx.PatientName.Should().Be("DOE^JOHN^MICHAEL");
    ctx.Dob.Should().Be(new DateOnly(1950, 1, 1));
    ctx.Sex.Should().Be("M");
    ctx.Modality.Should().Be("CT");
    ctx.Procedure.Should().Be("CT Chest with Contrast");
    ctx.PerformingSite.Should().Be("Main Radiology");
    ctx.OrderingProvider.Should().Be("SMITH^JANE^DR");
}
```

#### TEST-U-02: LaunchArgsContextProvider — Missing MRN Returns Null Context
```csharp
[Fact]
public void ReturnNullContextWhenMrnMissing()
{
    var args = new[] { "/acc=RAD-001", "/pt=DOE^JOHN" };
    var provider = new LaunchArgsContextProvider(args);
    provider.CurrentContext.Should().BeNull();
}
```

#### TEST-U-03: SafetyGuard — SameMrnAndAccession Returns No Mismatch
```csharp
[Fact]
public void SameMrnAndAccessionProducesNoMismatch()
{
    var guard = new SafetyGuard();
    var original = MakeContext(mrn: "111", acc: "ACC111");
    var same     = MakeContext(mrn: "111", acc: "ACC111");
    var result = guard.CheckContextChange(original, same);
    result.HasMismatch.Should().BeFalse();
    result.TriggersSafetyHold.Should().BeFalse();
}
```

#### TEST-U-04: SafetyGuard — MrnChangedTriggersSafetyHold
```csharp
[Fact]
public void MrnChangedTriggersSafetyHold()
{
    var guard = new SafetyGuard();
    var original = MakeContext(mrn: "111", acc: "ACC111");
    var changed  = MakeContext(mrn: "999", acc: "ACC111");
    var result = guard.CheckContextChange(original, changed);
    result.HasMismatch.Should().BeTrue();
    result.MismatchFields.Should().Contain("mrn");
    result.TriggersSafetyHold.Should().BeTrue();
}
```

#### TEST-U-05: SafetyGuard — AccessionChangedTriggersSafetyHold
```csharp
[Fact]
public void AccessionChangedTriggersSafetyHold()
{
    var guard = new SafetyGuard();
    var original = MakeContext(mrn: "111", acc: "ACC111");
    var changed  = MakeContext(mrn: "111", acc: "ACC999");
    var result = guard.CheckContextChange(original, changed);
    result.MismatchFields.Should().Contain("accession");
    result.TriggersSafetyHold.Should().BeTrue();
}
```

#### TEST-U-06: SafetyGuard — ApproveBlockedWhenMrnChanged
```csharp
[Fact]
public void ApproveIsBlockedWhenMrnChanged()
{
    var guard = new SafetyGuard();
    var session = MakeSession(
        originalCtx: MakeContext(mrn: "111", acc: "ACC111"),
        currentCtx:  MakeContext(mrn: "999", acc: "ACC111"),
        state: SessionState.Draft
    );
    var result = guard.ValidateApprove(session);
    result.IsValid.Should().BeFalse();
    result.Errors.Should().Contain(e => e.Contains("MRN changed"));
    result.RequiresOverride.Should().BeTrue();
}
```

#### TEST-U-07: SafetyGuard — ApproveSucceedsInFinalStateBlocked
```csharp
[Fact]
public void ApproveBlockedWhenStateIsAlreadyFinal()
{
    var guard = new SafetyGuard();
    var session = MakeSession(
        originalCtx: MakeContext(mrn: "111", acc: "ACC111"),
        currentCtx:  MakeContext(mrn: "111", acc: "ACC111"),
        state: SessionState.Final   // already final
    );
    var result = guard.ValidateApprove(session);
    result.IsValid.Should().BeFalse();
    result.Errors.Should().Contain(e => e.Contains("Invalid state"));
}
```

#### TEST-U-08: SafetyGuard — ApproveSucceedsFromDraftState
```csharp
[Fact]
public void ApproveSucceedsFromDraftWithMatchingContext()
{
    var guard = new SafetyGuard();
    var ctx = MakeContext(mrn: "111", acc: "ACC111");
    var session = MakeSession(originalCtx: ctx, currentCtx: ctx, state: SessionState.Draft);
    var result = guard.ValidateApprove(session);
    result.IsValid.Should().BeTrue();
    result.Errors.Should().BeEmpty();
}
```

#### TEST-U-09: DeviceManager — PairTokenExpiredAfterTtl
```csharp
[Fact]
public void PairTokenIsInvalidAfterTtlExpiry()
{
    var dm = new DeviceManager(new DeviceManagerOptions { PairTokenTtlSeconds = 1 });
    var sessionId = Guid.NewGuid();
    var token = dm.GeneratePairToken(sessionId, "DOMAIN\\rad");
    Thread.Sleep(1500); // exceed TTL
    var result = dm.ValidatePairToken(token, sessionId, "DOMAIN\\rad");
    result.IsValid.Should().BeFalse();
    result.FailureReason.Should().Be("TOKEN_EXPIRED");
}
```

#### TEST-U-10: DeviceManager — PairTokenCannotBeReused
```csharp
[Fact]
public void PairTokenCannotBeUsedTwice()
{
    var dm = new DeviceManager(new DeviceManagerOptions { PairTokenTtlSeconds = 60 });
    var sessionId = Guid.NewGuid();
    var token = dm.GeneratePairToken(sessionId, "DOMAIN\\rad");
    dm.ValidatePairToken(token, sessionId, "DOMAIN\\rad"); // first use
    var result = dm.ValidatePairToken(token, sessionId, "DOMAIN\\rad"); // replay
    result.IsValid.Should().BeFalse();
    result.FailureReason.Should().Be("TOKEN_REPLAYED");
}
```

#### TEST-U-11: Hl7OruBuilder — DraftMessageHasPreliminaryStatus
```csharp
[Fact]
public void DraftOruContainsPreliminaryStatusInObr25()
{
    var builder = new Hl7OruBuilder(LoadDefaultTemplate());
    var session = MakeFullSession();
    var oru = builder.BuildDraftOru(session, "Test report text");
    var segments = oru.Split('\r');
    var obr = segments.First(s => s.StartsWith("OBR|"));
    obr.Split('|')[25].Should().Be("P");
}
```

#### TEST-U-12: Hl7OruBuilder — FinalMessageHasFinalStatus
```csharp
[Fact]
public void FinalOruContainsFinalStatusInObr25()
{
    var builder = new Hl7OruBuilder(LoadDefaultTemplate());
    var session = MakeFullSession();
    var oru = builder.BuildFinalOru(session, "Test report text", "JOHNSON^MARK^DR");
    var obr = oru.Split('\r').First(s => s.StartsWith("OBR|"));
    obr.Split('|')[25].Should().Be("F");
}
```

#### TEST-U-13: Hl7OruBuilder — ReportTextSpecialCharsAreEscaped
```csharp
[Fact]
public void PipeCharacterInReportTextIsHl7Escaped()
{
    var builder = new Hl7OruBuilder(LoadDefaultTemplate());
    var session = MakeFullSession();
    var reportWithPipe = "Finding: normal|stable";
    var oru = builder.BuildDraftOru(session, reportWithPipe);
    // HL7 escape for | is \F\
    oru.Should().Contain(@"Finding: normal\F\stable");
    oru.Should().NotContain("normal|stable"); // raw pipe must not appear in OBX value
}
```

#### TEST-U-14: Hl7OruBuilder — NewlineInReportTextIsHl7Escaped
```csharp
[Fact]
public void NewlineInReportTextIsHl7Escaped()
{
    var builder = new Hl7OruBuilder(LoadDefaultTemplate());
    var session = MakeFullSession();
    var report = "FINDINGS:\rNormal.\rIMPRESSION:\rNo findings.";
    var oru = builder.BuildDraftOru(session, report);
    oru.Should().Contain(@"\X0D\");
}
```

#### TEST-U-15: OutboundQueue — EnqueuedMessageSurvivesRestart
```csharp
[Fact]
public void EnqueuedMessageIsPresentAfterQueueReinitialization()
{
    using var tempDir = new TempDirectory();
    var enc = new EncryptionService(GetTestKey());
    var queue = new OutboundQueue(tempDir.Path, enc);
    var msg = MakeOutboundMessage("MSG001");
    queue.Enqueue(msg);
    // Simulate restart
    var queue2 = new OutboundQueue(tempDir.Path, enc);
    var dequeued = queue2.Dequeue();
    dequeued.Should().NotBeNull();
    dequeued!.MessageId.Should().Be("MSG001");
}
```

#### TEST-U-16: OutboundQueue — InflightMessagesRecoveredOnStartup
```csharp
[Fact]
public void InflightMessagesAreMovedToPendingOnInit()
{
    using var tempDir = new TempDirectory();
    var enc = new EncryptionService(GetTestKey());
    // Manually put a file in inflight/
    var msg = MakeOutboundMessage("MSG_INFLIGHT");
    File.WriteAllBytes(
        Path.Combine(tempDir.Path, "inflight", "1_20250101_MSG_INFLIGHT.mda"),
        enc.Encrypt(JsonSerializer.SerializeToUtf8Bytes(msg))
    );
    var queue = new OutboundQueue(tempDir.Path, enc);
    // Should have recovered to pending
    queue.Dequeue().Should().NotBeNull();
}
```

#### TEST-U-17: AuditLog — MrnIsMaskedInLogEntry
```csharp
[Fact]
public void MrnIsNotLoggedInPlaintext()
{
    using var tempDir = new TempDirectory();
    var log = new AuditLog(tempDir.Path);
    log.Log(new AuditEvent
    {
        EventType = "TEST",
        MaskedMrn = AuditLog.MaskMrn("1234567")
    });
    var content = File.ReadAllText(Directory.GetFiles(tempDir.Path)[0]);
    content.Should().Contain("MRN-xxxx567");
    content.Should().NotContain("1234567"); // full MRN must not appear
}
```

#### TEST-U-18: AuditLog — PatientNameIsRedacted
```csharp
[Fact]
public void PatientNameIsRedactedInLogEntry()
{
    using var tempDir = new TempDirectory();
    var log = new AuditLog(tempDir.Path);
    var evt = new AuditEvent { EventType = "TEST" };
    // AuditEvent has no PatientName field — verify no way to log it
    // Check that the PHI masker replaces any raw name accidentally passed in Details
    evt = evt with { Details = AuditLog.MaskFreeText("Patient John Doe has...") };
    log.Log(evt);
    var content = File.ReadAllText(Directory.GetFiles(tempDir.Path)[0]);
    content.Should().NotContain("John Doe");
}
```

#### TEST-U-19: Hl7Sender — MllpFramingIsCorrect
```csharp
[Fact]
public async Task MllpSenderWritesCorrectStartBlockAndEndBlock()
{
    var buffer = new List<byte>();
    var mockStream = new MockNetworkStream(buffer);
    var sender = new MllpSender(mockStream);
    await sender.SendRawAsync("MSH|...\rPID|...\r");
    buffer[0].Should().Be(0x0B);  // VT (start block)
    buffer[^2].Should().Be(0x1C); // FS (end block)
    buffer[^1].Should().Be(0x0D); // CR
}
```

#### TEST-U-20: SessionManager — OnlyOneActiveSessionPerUser
```csharp
[Fact]
public void SecondCreateSessionForSameUserClosesFirst()
{
    var mgr = new SessionManager();
    var ctx = MakeContext(mrn: "111", acc: "ACC111");
    var s1 = mgr.CreateSession(ctx, "DOMAIN\\rad");
    var s2 = mgr.CreateSession(MakeContext(mrn: "222", acc: "ACC222"), "DOMAIN\\rad");

    mgr.GetActiveSession("DOMAIN\\rad")!.SessionId.Should().Be(s2.SessionId);
    s1.State.Should().Be(SessionState.Closed); // first session auto-closed
}
```

---

### 8.2 Integration and UAT Scenarios (15 Scenarios)

#### SCENARIO FORMAT
```
ID:          UAT-##
Title:       <name>
Precondition: <state before test>
Steps:       <numbered actions>
Expected:    <expected outcome>
Verify:      <what to check in logs/HL7/UI>
```

---

#### UAT-01: Full Happy Path — Read → Draft → Approve → Final
```
ID: UAT-01
Title: Full happy-path dictation and approval
Precondition: Agent running; Centricity launched with valid MRN+ACC; phone not yet paired
Steps:
  1. Open exam in Centricity, click Dictate
  2. Agent receives context via launch args
  3. QR appears on PC screen (system tray)
  4. Rad scans QR with phone
  5. Phone shows correct patient name, MRN, accession, modality, procedure
  6. Rad taps READ
  7. Rad taps START DICTATION; dictates for ~30 seconds
  8. Rad taps STOP DICTATION
  9. Phone shows DRAFT badge
  10. Rad reviews, taps APPROVE
  11. Phone shows FINAL badge with green indicator
Expected:
  - HL7 ORU^R01 with OBR-25=F written to drop folder / sent via MLLP
  - ACK received (AA)
  - Audit log contains PAIR_OK, READ_STARTED, DICTATION_STARTED, DICTATION_STOPPED, APPROVE_OK, ORU_SENT, ORU_ACK
Verify:
  - HL7 file: correct MRN, ACC, patient name, OBR-25=F
  - No PHI in text logs (only masked forms)
  - Phone shows FINAL within 3s of APPROVE tap
```

#### UAT-02: Context Change Mid-Dictation
```
ID: UAT-02
Title: Safety stop on context change during active dictation
Precondition: Active session in DICTATING state (MRN=111, ACC=ACC111)
Steps:
  1. While phone is recording, click a different patient in Centricity
  2. CCOW publishes new context (MRN=999, ACC=ACC999)
Expected:
  - Agent detects mismatch within 500ms
  - Dictation stops immediately (no ORU sent for incomplete dictation)
  - Phone shows full-screen red warning banner: "PATIENT CONTEXT CHANGED — Dictation Stopped"
  - All workflow buttons disabled
  - Audit log: CONTEXT_MISMATCH, SAFETY_HOLD
Verify:
  - No ORU file/MLLP message generated for the incomplete dictation
  - Banner remains until rad taps "I UNDERSTAND"
  - Returning to original exam in Centricity resolves SAFETY_HOLD and phone shows "Resume?"
```

#### UAT-03: Approve Blocked — Context Mismatch Present
```
ID: UAT-03
Title: Approve button blocked when MRN has changed since session start
Precondition: Session started with MRN=111; context changed to MRN=999; state is DRAFT
Steps:
  1. Rad taps APPROVE without context being restored
Expected:
  - Agent returns WORKFLOW_ACTION_REJECTED with reason=CONTEXT_MISMATCH
  - Phone shows "Approve blocked: patient context has changed"
  - Audit log: APPROVE_BLOCKED
  - No ORU generated
Verify:
  - HL7 drop folder / MLLP listener shows no new message
  - Audit entry present with APPROVE_BLOCKED event
```

#### UAT-04: Controlled Override — Approve With Override Reason
```
ID: UAT-04
Title: Approve with explicit controlled override (acknowledged context mismatch)
Precondition: Same as UAT-03 (CONTEXT_MISMATCH present)
Steps:
  1. Rad taps APPROVE → blocked
  2. Rad enters override reason: "Confirmed correct patient; CCOW data stale after system restart"
  3. Rad confirms override
  4. Phone sends WorkflowAction{action:APPROVE, confirmOverride:true, overrideReason:"..."}
Expected:
  - Agent accepts override
  - ORU generated and sent with OBR-25=F
  - Audit log: APPROVE_OVERRIDE with full reason text preserved
Verify:
  - AuditEvent.OverrideReason contains the typed reason
  - HL7 FINAL sent and ACKed
```

#### UAT-05: QR Token Expiry
```
ID: UAT-05
Title: Pairing fails if QR token has expired (>60s)
Precondition: Agent shows QR on screen
Steps:
  1. Wait 65 seconds without scanning
  2. Scan the original QR
Expected:
  - Agent returns HTTP 401 with code=TOKEN_EXPIRED
  - Phone shows "QR code has expired. Ask workstation to refresh."
  - QR on PC auto-regenerates after 60s for convenience
Verify:
  - No session created
  - Audit log: PAIR_FAILED, reason=TOKEN_EXPIRED
```

#### UAT-06: Device Revocation Mid-Session
```
ID: UAT-06
Title: Revoked device is immediately disconnected
Precondition: Active paired session; phone in READING state
Steps:
  1. IT admin revokes the device from the admin panel (or Agent tray menu)
Expected:
  - Agent pushes ERROR_NOTIFICATION{code:DEVICE_REVOKED, severity:FATAL} to phone
  - WebSocket closed by Agent
  - Phone shows "This device has been revoked by your IT administrator. Contact helpdesk."
  - Session closed and state persisted as CLOSED (no data loss for completed dictations)
Verify:
  - Device appears as revoked in device registry
  - Subsequent pair attempts with same deviceId return 403 DEVICE_REVOKED
  - Audit log: DEVICE_REVOKED, SESSION_CLOSED
```

#### UAT-07: HL7 MLLP NAK → Retry → Dead-Letter
```
ID: UAT-07
Title: HL7 send fails 4 times and lands in dead-letter queue
Precondition: FINAL approved; MLLP server configured to return NAK for testing
Steps:
  1. Configure test MLLP server to return AE for all messages
  2. Approve a dictation
  3. Wait for all 4 retry attempts (at 0s, 5s, 30s, 120s intervals)
Expected:
  - After attempt 4, message moves to dead-letter/
  - Windows tray balloon: "HL7 send failed after 4 attempts — IT alert"
  - Phone shows StatusUpdate{hl7Status: DEAD_LETTER}
Verify:
  - Dead-letter folder contains 1 encrypted file for this message
  - Audit log: ORU_BUILT, ORU_SENT (attempt 1-4), ORU_DEAD_LETTER
  - No PHI in log entries, only masked identifiers
```

#### UAT-08: Agent Crash Recovery
```
ID: UAT-08
Title: Agent restarts and recovers queued messages without loss
Precondition: Approved FINAL; ORU in pending queue; MLLP server unavailable
Steps:
  1. Kill Agent process (Task Manager)
  2. Start Agent again
  3. Bring MLLP server online
Expected:
  - On startup, Agent moves any inflight/ files back to pending/
  - HL7 sender picks up pending message and successfully sends
  - ACK received and message moved to sent/
Verify:
  - No duplicate messages sent
  - Audit log shows ORU_SENT and ORU_ACK after restart
```

#### UAT-09: Audio Streaming — Reconnect During Dictation
```
ID: UAT-09
Title: Phone reconnects mid-dictation and audio resumes without gap
Precondition: Active dictation; ~20 chunks sent
Steps:
  1. Simulate Wi-Fi disconnect on phone (toggle airplane mode momentarily)
  2. Phone reconnects within 10s
Expected:
  - Phone client detects disconnect via heartbeat timeout
  - Phone shows brief "Reconnecting..." indicator
  - On reconnect, phone resends any unacknowledged chunks (by sequence number)
  - Agent assembles complete audio stream without duplicates (sequence dedup)
Verify:
  - Final audio file on Agent contains no perceptible gap
  - No sequence numbers missing or duplicated
  - Audit log: DICTATION_STARTED, AUDIO_RECONNECT, DICTATION_STOPPED
```

#### UAT-10: Co-read Workflow
```
ID: UAT-10
Title: Primary read → co-read → final approve
Precondition: Session in DRAFT state after first dictation
Steps:
  1. Rad taps CO-READ
  2. Phone shows SECOND_READ_PENDING badge
  3. Rad starts second dictation (addendum)
  4. Rad stops second dictation
  5. HL7 ORU with OBR-25=S and CO-READ annotation sent (or local config)
  6. Rad taps APPROVE
  7. FINAL ORU sent with OBR-25=F
Expected:
  - Two ORU messages generated: one SECOND_READ_PENDING (OBR-25=S), one FINAL (OBR-25=F)
  - Audit: CO_READ_STARTED, DICTATION_STARTED, DICTATION_STOPPED, APPROVE_OK
Verify:
  - HL7 FINAL OBX contains co-reader name in OBX-3
```

#### UAT-11: No Context Available — Agent Idles
```
ID: UAT-11
Title: Agent refuses pairing when no clinical context is available
Precondition: Agent running; Centricity not launched; no CCOW context
Steps:
  1. Phone tries to scan QR (no QR is shown — Agent is idle)
  2. Manually type Agent URL and call /pair without valid context
Expected:
  - Agent returns HTTP 503 with code=NO_ACTIVE_CONTEXT
  - Phone shows "No active patient context on workstation"
Verify:
  - No session created
  - Audit log: PAIR_FAILED, reason=NO_ACTIVE_CONTEXT
```

#### UAT-12: PHI Leakage Audit
```
ID: UAT-12
Title: PHI is not present in plaintext logs after a full session
Precondition: None
Steps:
  1. Complete a full session (pair → read → dictate → approve → final → close)
  2. Inspect all log files in %ProgramData%\MDA\logs\
  3. Inspect audit files in %ProgramData%\MDA\audit\
  4. Search for the real MRN, patient name, DOB, accession number
Expected:
  - MRN appears only as "MRN-xxxx<last4>"
  - Patient name never appears (always "[REDACTED]" or absent)
  - DOB appears only as year (or absent)
  - Accession appears masked
  - Full PHI is only in encrypted queue files (not readable without DPAPI key)
Verify:
  - grep for known MRN in logs/ returns 0 results
  - grep for patient surname in logs/ returns 0 results
```

#### UAT-13: Approve Without Prior Read or Dictation
```
ID: UAT-13
Title: Approve is rejected if no dictation has occurred
Precondition: Session in READY state (paired but READ not tapped)
Steps:
  1. Rad taps APPROVE directly without tapping READ or DICTATION
Expected:
  - Agent returns WORKFLOW_ACTION_REJECTED, reason=INVALID_STATE
  - Phone shows "Dictation has not been started for this case"
  - No ORU generated
Verify:
  - Audit log: APPROVE_BLOCKED, reason=INVALID_STATE
```

#### UAT-14: Multiple Sessions — Different Users Same Machine
```
ID: UAT-14
Title: Two radiologists pair simultaneously on the same workstation
Precondition: Agent supports multi-user mode (Phase 3); two RIS sessions open
Steps:
  1. User A opens exam MRN=111, Agent creates session for DOMAIN\rad1
  2. User B opens exam MRN=222, Agent creates session for DOMAIN\rad2
  3. Phone-A pairs to session for rad1, Phone-B pairs to session for rad2
Expected:
  - Each phone sees only its own patient context
  - Audio streams are kept isolated per session
  - SafetyGuard checks are isolated per session (context change for rad1 doesn't affect rad2)
Verify:
  - Phone-A: shows MRN=111 only
  - Phone-B: shows MRN=222 only
  - Two separate HL7 ORU messages generated and sent
```

#### UAT-15: HL7 Report Text Long — Split Across Multiple OBX
```
ID: UAT-15
Title: Report text exceeding OBX value length limit is correctly split
Precondition: None
Steps:
  1. Generate a report text of 70,000 characters (simulating a very detailed CT report)
  2. Approve dictation
Expected:
  - HL7OruBuilder splits text across multiple OBX segments (OBX|1|TX... OBX|2|TX... etc.)
  - Each OBX value is ≤ 65535 characters
  - All OBX segments are contiguous and numbered correctly
  - Combined text when reassembled by downstream system matches original
Verify:
  - HL7 message passes HL7 validator (e.g., HAPI Fhir CLI) without errors
  - Downstream RIS/LIS displays complete report text
```
