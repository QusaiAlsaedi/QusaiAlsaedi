# Mobile Dictation Agent — Threat Model Checklist

## 9. Threat Model Checklist

**Framework:** STRIDE (Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege)

---

### 9.1 PHI Leakage

| # | Threat | Vector | Mitigation | Residual Risk |
|---|---|---|---|---|
| T-01 | PHI logged in plaintext | Agent logs context fields verbatim | Serilog destructuring policy masks MRN, name, DOB. AuditLog only stores masked fields. Code review gate: no `log.Info(context.Mrn)`. | Low — requires code review bypass |
| T-02 | PHI stored on phone disk | App writes context to AsyncStorage or file | PHI held in-memory Zustand store only. No file writes. Cleared on background >2 min. Tested via memory dump + device file scan in QA. | Low |
| T-03 | PHI in crash reports | Crash reporter (Sentry, Firebase) captures stack with PHI in memory | Phase 3 uses Sentry with `beforeSend` scrub of all PHI fields. Phase 1–2: no external crash reporting. | Medium — mitigated in Phase 3 |
| T-04 | PHI in queue files at rest | Queue files readable by local admin | AES-256-GCM encryption keyed by DPAPI (machine scope). Files are opaque blobs. | Low — requires local admin + Agent source |
| T-05 | PHI in backups | Windows Backup / VSS copies encrypted files | Encrypted blobs backed up — still safe. Key is in DPAPI which is machine-scoped. | Low |
| T-06 | PHI in dead-letter queue readable by IT | IT accesses dead-letter for manual review | Dead-letter files are encrypted. Admin endpoint provides decrypted view only to local authenticated admin. Log entries show masked identifiers only. | Low |
| T-07 | PHI in TLS traffic captured on network | Network sniffing between phone and Agent | TLS 1.2+ enforced. Certificate pinned on phone side. No cleartext fallback. | Low |
| T-08 | PHI in audio file on phone | Audio chunks buffered to phone disk | Streaming only — chunks are sent via WebSocket and not written to disk. Pending map in memory only. | Low |
| T-09 | PHI in agent memory dump | Process memory captured (e.g., by malware) | Defense in depth: endpoint AV, no PHI in heap strings beyond active session lifetime. SecureString for JWT signing key. Mitigated at OS level. | Medium — standard for Windows process |

---

### 9.2 Stolen Phone

| # | Threat | Mitigation | Residual Risk |
|---|---|---|---|
| T-10 | Thief reads patient data on unlocked phone | PHI in memory only; iOS locks memory on screen lock; all PHI cleared on device lock event | Low — requires unlocked phone |
| T-11 | Thief uses SessionToken from phone to query Agent | SessionToken is stored in iOS Keychain / Android EncryptedSharedPreferences (hardware-backed). Agent validates JWT + deviceId. Stolen token usable until 8h expiry or device revocation. | Medium — mitigated by device revocation |
| T-12 | Thief re-pairs using stolen phone | DeviceId from secure enclave. New pair requires valid PairToken from QR on PC (physical access required). | Low — physical access required |
| T-13 | Thief forces app to reconnect to rogue Agent | Certificate pinning prevents connecting to non-pinned Agent. New QR required for each session. | Low |
| T-14 | Thief harvests audio | Audio on phone: in-memory streaming only. On Agent: encrypted at rest, auto-deleted after 24h. | Low |

**Operational response for stolen phone:**
1. IT admin opens Agent admin panel → revoke device by deviceId
2. Phone disconnects within 5s (next heartbeat)
3. deviceId added to revocation list in encrypted SQLite
4. Audit log: DEVICE_REVOKED

---

### 9.3 Man-in-the-Middle (MITM)

| # | Threat | Mitigation | Residual Risk |
|---|---|---|---|
| T-15 | Rogue Access Point intercepts WebSocket traffic | TLS 1.2+. Certificate fingerprint embedded in QR and pinned on phone. MITM cert would have different fingerprint → connection refused. | Low |
| T-16 | DNS poisoning redirects phone to rogue server | QR payload contains raw IP address + cert fingerprint. No DNS lookup. | Low |
| T-17 | SSL stripping | App uses WSS (WebSocket Secure). No cleartext WS permitted by Agent config. Phone rejects non-TLS connections at code level. | Low |
| T-18 | MLLP traffic intercepted | MLLP uses TLS (`SslStream`). Hospital network should segment HL7 traffic. | Medium — depends on hospital network |
| T-19 | Rogue Agent on LAN responds to phone | Phone uses pinned cert from QR. Rogue Agent has different cert → handshake fails. | Low |

---

### 9.4 Replay Attacks

| # | Threat | Mitigation | Residual Risk |
|---|---|---|---|
| T-20 | PairToken replayed | HMAC-SHA256 token is single-use. Nonce tracking in memory (and SQLite for persistence). Second use returns `TOKEN_REPLAYED`. TTL 60s. | Low |
| T-21 | SessionToken replayed after session close | JWT has `jti` (unique ID). On session close, `jti` added to short-term revocation list (10 min retention). Agent validates `jti` against revocation. | Low |
| T-22 | Audio chunks replayed to inject audio | Sequence numbers tracked per `dictationId`. Duplicate sequences are discarded by Agent. `dictationId` is UUID per session, so cross-session replay is invalid. | Low |
| T-23 | WORKFLOW_ACTION replayed (e.g., Approve twice) | `msgId` tracked per session. Duplicate `msgId` returns `DUPLICATE_MESSAGE` error. Session state machine also blocks repeat Approve. | Low |
| T-24 | HL7 message replayed to downstream | HL7 `MSH-10` (message control ID) = UUID. Downstream interface engine should dedup (standard behavior). Agent does not replay already-ACKed messages. | Low — depends on downstream dedup |

---

### 9.5 Logging and Audit

| # | Threat | Mitigation | Residual Risk |
|---|---|---|---|
| T-25 | Audit log tampered or deleted | Audit files are write-only (ACL: SYSTEM write, Administrators read). No delete API. Append-only (Serilog). WORM storage option for Phase 3. | Medium — local admin can still delete |
| T-26 | PHI injected into audit log via report text | Report text is not logged. Audit log entries contain only masked identifiers and event types. | Low |
| T-27 | Audit log not capturing override events | Override events are mandatorily logged in SafetyGuard. Unit test (TEST-U-06) verifies this. Code review gate. | Low |
| T-28 | Log rotation deletes old audit records | Configured retention: 7 years (2555 days). IT policy should verify backup of audit folder. | Low with proper backup policy |

---

### 9.6 Offline Mode

| # | Threat | Mitigation | Residual Risk |
|---|---|---|---|
| T-29 | Agent offline — phone cannot reach Agent | Phone heartbeat detects disconnect within 25s (15s interval + 10s timeout). Shows "Connection lost" banner. Dictation auto-stopped if active. No PHI at risk since phone has no offline storage. | Low |
| T-30 | Agent offline — HL7 not delivered | OutboundQueue persists approved ORU messages encrypted. When Agent restarts, messages are retried. Maximum gap = Agent downtime. | Low if Agent downtime < 24h |
| T-31 | Phone dictates while disconnected from Agent | Not possible by design. Audio capture requires active WebSocket connection. If disconnected mid-dictation, `StopDictation{reason:NETWORK_LOSS}` sent on reconnect. | Low |
| T-32 | Rad approves while Agent temporarily offline | Approve call fails immediately (WS send fails). Phone shows "Unable to send — check connection." No ORU generated. Rad retries when connection restored. | Low |

---

### 9.7 Elevation of Privilege

| # | Threat | Mitigation | Residual Risk |
|---|---|---|---|
| T-33 | Phone app escalates to admin on Agent | Admin endpoints are on separate port 8444 (Phase 3). Admin JWT is different from session JWT — issued only to localhost requests with Windows admin credentials. | Low |
| T-34 | Unpaired phone sends workflow actions | All WebSocket messages require valid SessionToken (JWT). Token requires prior pair which requires physical QR scan. Unauthenticated messages return 401 and close WS. | Low |
| T-35 | Session token used for different session | JWT contains `sid` (sessionId). Agent validates that `sid` matches the active session for the deviceId. Cross-session token use returns 403. | Low |
| T-36 | Injection via patientName / reportText into HL7 | HL7OruBuilder HTML-encodes all template inputs and applies HL7 escape sequences. Pipe, caret, tilde, ampersand, backslash in report text are all escaped. Fuzz tested. | Low |

---

### 9.8 Denial of Service

| # | Threat | Mitigation | Residual Risk |
|---|---|---|---|
| T-37 | Flood pairing requests (brute force QR) | Rate limit: 5 pair attempts per IP per minute. PairToken is HMAC — brute force of 256-bit HMAC is infeasible. | Low |
| T-38 | Flood audio chunks — overflow Agent memory | Agent audio buffer limited to 50 chunks per session (~12.5s). Overflow triggers `AUDIO_BUFFER_OVERFLOW` notification and stops mic. | Low |
| T-39 | Large report text via phone to crash Agent | Report text field capped at 1 MB per message (Agent schema validator). Larger messages are rejected with `SCHEMA_VALIDATION_FAILED`. | Low |
| T-40 | Phone disconnects and reconnects rapidly | Exponential backoff on phone side (1s, 2s, 4s... up to 30s). Agent rate-limits reconnects: >10 reconnects/min from same device IP closes the device session. | Low |

---

### 9.9 HIPAA / Regulatory Notes

| Requirement | Implementation |
|---|---|
| **PHI encryption at rest** | AES-256-GCM for queue + audio files; DPAPI for key protection |
| **PHI encryption in transit** | TLS 1.2+ for all WebSocket and MLLP connections |
| **Access controls** | SessionToken scoped to single user; admin token separate; device revocation |
| **Audit logging** | All PHI access events logged with masked identifiers; 7-year retention |
| **Minimum necessary** | Phone receives only the context fields required for UI display; no history |
| **Breach notification** | Stolen device + Device revocation workflow covers rapid response |
| **Risk assessment** | This threat model serves as input to annual HIPAA risk assessment update |
