# Mobile Dictation Agent — API Contract

## 3. API Contract: Phone ↔ Agent

### 3.1 Transport

| Property | Value |
|---|---|
| Protocol | HTTPS (REST for pairing) + WSS (WebSocket for session) |
| TLS version | TLS 1.2 minimum, TLS 1.3 preferred |
| Agent TLS cert | Self-signed with pinned SHA-256 fingerprint embedded in QR payload |
| Base URL | `wss://{agentIp}:8443/mda/v1` |
| Pairing endpoint | `POST https://{agentIp}:8443/mda/v1/pair` |

---

### 3.2 Authentication Scheme

#### Pairing Phase (one-time)
```
POST /mda/v1/pair
Authorization: Bearer {PairToken}    ← from QR code, 60s TTL
Content-Type: application/json
```

#### Session Phase (all subsequent messages)
```
Authorization: Bearer {SessionToken}   ← JWT issued by Agent on pair success
```

**JWT Claims:**
```json
{
  "iss": "mda-agent",
  "sub": "{deviceId}",
  "sid": "{sessionId}",
  "win": "DOMAIN\\radiologist",
  "exp": 1740000000,
  "iat": 1739971200,
  "jti": "{uuid}"
}
```
- Signed with HS256 using a machine-scoped secret (DPAPI-protected)
- Token lifetime: 8 hours (configurable)
- Included in every WebSocket message as `token` field OR in the initial WS upgrade header

---

### 3.3 WebSocket Message Envelope

All messages (both directions) use this JSON wrapper:

```jsonc
{
  "msgId":   "uuid-v4",          // for correlation / dedup
  "type":    "string",           // message type (see below)
  "token":   "eyJ...",           // SessionToken (Phone→Agent only)
  "ts":      "ISO8601",          // sender timestamp
  "payload": { ... }             // type-specific content
}
```

---

### 3.4 Message Schemas

#### 3.4.1 PairRequest (Phone → Agent, REST POST body)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "PairRequest",
  "type": "object",
  "required": ["pairToken", "deviceId", "deviceOS", "appVersion", "ecdhPublicKey"],
  "properties": {
    "pairToken": {
      "type": "string",
      "description": "Short-lived token from QR code"
    },
    "deviceId": {
      "type": "string",
      "pattern": "^[a-f0-9-]{36}$",
      "description": "Stable device UUID (generated once, stored in secure enclave)"
    },
    "deviceOS": {
      "type": "string",
      "enum": ["ios", "android"],
      "description": "Operating system of phone"
    },
    "deviceModel": {
      "type": "string",
      "description": "Model string (e.g. iPhone 15 Pro). Optional, for audit."
    },
    "appVersion": {
      "type": "string",
      "pattern": "^\\d+\\.\\d+\\.\\d+$"
    },
    "ecdhPublicKey": {
      "type": "string",
      "description": "Base64-encoded ECDH P-256 public key for channel key agreement"
    }
  },
  "additionalProperties": false
}
```

#### 3.4.2 PairResponse (Agent → Phone, REST response)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "PairResponse",
  "type": "object",
  "required": ["sessionToken", "sessionId", "context", "serverEcdhPublicKey", "agentVersion"],
  "properties": {
    "sessionToken": {
      "type": "string",
      "description": "JWT for all subsequent WebSocket messages"
    },
    "sessionId": {
      "type": "string",
      "description": "UUID of the active session"
    },
    "context": {
      "$ref": "#/definitions/ClinicalContext"
    },
    "serverEcdhPublicKey": {
      "type": "string",
      "description": "Base64-encoded ECDH P-256 public key (Agent side)"
    },
    "agentVersion": {
      "type": "string"
    },
    "wsEndpoint": {
      "type": "string",
      "description": "Full WSS URL for subsequent connection (may differ from pair URL)"
    }
  },
  "definitions": {
    "ClinicalContext": {
      "type": "object",
      "required": ["mrn", "accession", "patientName", "dob", "sex", "modality", "procedure", "orderDateTime"],
      "properties": {
        "mrn":              { "type": "string" },
        "accession":        { "type": "string" },
        "patientName":      { "type": "string", "description": "Family^Given^Middle" },
        "dob":              { "type": "string", "format": "date", "description": "YYYY-MM-DD" },
        "sex":              { "type": "string", "enum": ["M", "F", "O", "U"] },
        "modality":         { "type": "string" },
        "procedure":        { "type": "string" },
        "orderDateTime":    { "type": "string", "format": "date-time" },
        "performingSite":   { "type": "string" },
        "orderingProvider": { "type": "string" }
      }
    }
  },
  "additionalProperties": false
}
```

#### 3.4.3 ContextUpdate (Agent → Phone, WebSocket push)

Pushed whenever the Agent detects a context change.

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ContextUpdate",
  "type": "object",
  "required": ["type", "ts", "msgId", "payload"],
  "properties": {
    "type": { "type": "string", "enum": ["CONTEXT_UPDATE"] },
    "ts":   { "type": "string", "format": "date-time" },
    "msgId":{ "type": "string" },
    "payload": {
      "type": "object",
      "required": ["changeType", "newContext"],
      "properties": {
        "changeType": {
          "type": "string",
          "enum": ["FULL_REPLACE", "MRN_CHANGED", "ACCESSION_CHANGED", "CONTEXT_CLEARED"]
        },
        "newContext":   { "$ref": "#/definitions/ClinicalContext" },
        "mismatchFields": {
          "type": "array",
          "items": { "type": "string" },
          "description": "Fields that differ from session context: ['mrn','accession']"
        },
        "safetyBlock": {
          "type": "boolean",
          "description": "True if Agent has halted dictation due to mismatch"
        }
      }
    }
  }
}
```

#### 3.4.4 StartDictation (Phone → Agent, WebSocket)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "StartDictation",
  "type": "object",
  "required": ["type", "token", "ts", "msgId", "payload"],
  "properties": {
    "type":  { "type": "string", "enum": ["START_DICTATION"] },
    "token": { "type": "string" },
    "ts":    { "type": "string", "format": "date-time" },
    "msgId": { "type": "string" },
    "payload": {
      "type": "object",
      "required": ["audioFormat", "sampleRate", "channels"],
      "properties": {
        "audioFormat": {
          "type": "string",
          "enum": ["opus", "pcm_s16le"],
          "description": "Opus preferred for bandwidth. PCM for compatibility."
        },
        "sampleRate": {
          "type": "integer",
          "enum": [8000, 16000, 44100],
          "description": "16000 Hz recommended for STT"
        },
        "channels": {
          "type": "integer",
          "enum": [1],
          "description": "Mono only"
        },
        "chunkDurationMs": {
          "type": "integer",
          "default": 250,
          "description": "Audio chunk size in milliseconds"
        }
      }
    }
  }
}
```

**Response (Agent → Phone):**
```json
{
  "type": "START_DICTATION_ACK",
  "msgId": "uuid",
  "ts": "ISO8601",
  "payload": {
    "dictationId": "uuid",
    "state": "DICTATING",
    "accepted": true
  }
}
```

#### 3.4.5 AudioChunk (Phone → Agent, WebSocket binary frame)

Binary WebSocket frames are used for audio chunks to minimize overhead.

**Frame structure:**
```
Bytes 0-35:  dictationId (UTF-8, 36 bytes, fixed width)
Bytes 36-39: sequenceNumber (uint32 big-endian)
Bytes 40-43: chunkDurationMs (uint32 big-endian)
Bytes 44+:   audio payload (Opus or PCM)
```

**JSON fallback (if binary frames are unavailable):**
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "AudioChunkMessage",
  "type": "object",
  "required": ["type", "token", "ts", "msgId", "payload"],
  "properties": {
    "type":  { "type": "string", "enum": ["AUDIO_CHUNK"] },
    "token": { "type": "string" },
    "ts":    { "type": "string", "format": "date-time" },
    "msgId": { "type": "string" },
    "payload": {
      "type": "object",
      "required": ["dictationId", "sequenceNumber", "audioBase64"],
      "properties": {
        "dictationId":    { "type": "string" },
        "sequenceNumber": { "type": "integer", "minimum": 0 },
        "chunkDurationMs":{ "type": "integer" },
        "audioBase64":    { "type": "string", "description": "Base64-encoded audio data" },
        "isFinal":        { "type": "boolean", "default": false }
      }
    }
  }
}
```

#### 3.4.6 StopDictation (Phone → Agent, WebSocket)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "StopDictation",
  "type": "object",
  "required": ["type", "token", "ts", "msgId", "payload"],
  "properties": {
    "type":  { "type": "string", "enum": ["STOP_DICTATION"] },
    "token": { "type": "string" },
    "ts":    { "type": "string", "format": "date-time" },
    "msgId": { "type": "string" },
    "payload": {
      "type": "object",
      "required": ["dictationId"],
      "properties": {
        "dictationId": { "type": "string" },
        "reason": {
          "type": "string",
          "enum": ["USER_STOPPED", "SAFETY_HOLD", "NETWORK_LOSS", "APP_BACKGROUND"],
          "default": "USER_STOPPED"
        }
      }
    }
  }
}
```

**Response (Agent → Phone):**
```json
{
  "type": "STOP_DICTATION_ACK",
  "msgId": "uuid",
  "ts": "ISO8601",
  "payload": {
    "dictationId": "uuid",
    "state": "DRAFT",
    "durationSeconds": 147,
    "chunksReceived": 589,
    "missingSequences": []
  }
}
```

#### 3.4.7 WorkflowAction (Phone → Agent, WebSocket)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "WorkflowAction",
  "type": "object",
  "required": ["type", "token", "ts", "msgId", "payload"],
  "properties": {
    "type":  { "type": "string", "enum": ["WORKFLOW_ACTION"] },
    "token": { "type": "string" },
    "ts":    { "type": "string", "format": "date-time" },
    "msgId": { "type": "string" },
    "payload": {
      "type": "object",
      "required": ["action"],
      "properties": {
        "action": {
          "type": "string",
          "enum": ["READ", "CO_READ", "APPROVE"]
        },
        "confirmOverride": {
          "type": "boolean",
          "default": false,
          "description": "Set true when radiologist explicitly confirms controlled override for APPROVE. Triggers full audit entry."
        },
        "overrideReason": {
          "type": "string",
          "maxLength": 500,
          "description": "Required if confirmOverride=true. Freetext reason."
        }
      }
    }
  }
}
```

**Response (Agent → Phone):**
```json
{
  "type": "WORKFLOW_ACTION_ACK",
  "msgId": "uuid",
  "ts": "ISO8601",
  "payload": {
    "action": "APPROVE",
    "accepted": true,
    "newState": "FINAL",
    "hl7MsgId": "uuid",
    "hl7Status": "QUEUED"
  }
}
```

**Rejection response (safety block):**
```json
{
  "type": "WORKFLOW_ACTION_REJECTED",
  "msgId": "uuid",
  "ts": "ISO8601",
  "payload": {
    "action": "APPROVE",
    "reason": "CONTEXT_MISMATCH",
    "description": "Patient context changed since session start. Approve blocked.",
    "requiresConfirmOverride": true
  }
}
```

#### 3.4.8 StatusUpdate (Agent → Phone, WebSocket push)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "StatusUpdate",
  "type": "object",
  "required": ["type", "ts", "msgId", "payload"],
  "properties": {
    "type":  { "type": "string", "enum": ["STATUS_UPDATE"] },
    "ts":    { "type": "string", "format": "date-time" },
    "msgId": { "type": "string" },
    "payload": {
      "type": "object",
      "required": ["state"],
      "properties": {
        "state": {
          "type": "string",
          "enum": [
            "READY", "READING", "DICTATING", "DRAFT",
            "SECOND_READ_PENDING", "FINAL",
            "SAFETY_HOLD", "SAFETY_HOLD_RESOLVED",
            "DISCARDED", "ERROR"
          ]
        },
        "hl7Status": {
          "type": "string",
          "enum": ["NOT_SENT", "QUEUED", "SENT", "ACK_RECEIVED", "NAK_RECEIVED", "DEAD_LETTER"]
        },
        "ackCode":    { "type": "string" },
        "canResume":  { "type": "boolean" },
        "message":    { "type": "string", "description": "Human-readable status message" }
      }
    }
  }
}
```

#### 3.4.9 ErrorNotification (Agent → Phone, WebSocket push)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ErrorNotification",
  "type": "object",
  "required": ["type", "ts", "msgId", "payload"],
  "properties": {
    "type":  { "type": "string", "enum": ["ERROR_NOTIFICATION"] },
    "ts":    { "type": "string", "format": "date-time" },
    "msgId": { "type": "string" },
    "payload": {
      "type": "object",
      "required": ["code", "severity"],
      "properties": {
        "code": {
          "type": "string",
          "enum": [
            "CONTEXT_CHANGED",
            "CONTEXT_CLEARED",
            "HL7_SEND_FAILED",
            "HL7_NAK_RECEIVED",
            "AUDIO_BUFFER_OVERFLOW",
            "SESSION_EXPIRED",
            "DEVICE_REVOKED",
            "SAFETY_BLOCK_APPROVE",
            "INTERNAL_ERROR"
          ]
        },
        "severity": {
          "type": "string",
          "enum": ["INFO", "WARNING", "HARD_BLOCK", "FATAL"]
        },
        "message":     { "type": "string" },
        "retryable":   { "type": "boolean" },
        "requiresAck": {
          "type": "boolean",
          "description": "True means phone must show blocking modal requiring explicit radiologist acknowledgment"
        },
        "detail":      { "type": "object", "description": "Additional non-PHI context" }
      }
    }
  }
}
```

#### 3.4.10 Heartbeat (bidirectional, every 15s)

```json
{
  "type":  "HEARTBEAT",
  "msgId": "uuid",
  "ts":    "ISO8601",
  "payload": {
    "seqNum": 42
  }
}
```
Response: same structure with `"type": "HEARTBEAT_ACK"`. If no ACK within 10s, trigger reconnect.

---

### 3.5 Error Codes (HTTP layer, pairing only)

| HTTP Status | Body `code` | Meaning |
|---|---|---|
| 401 | `TOKEN_EXPIRED` | PairToken TTL exceeded |
| 401 | `TOKEN_INVALID` | Bad signature |
| 409 | `DEVICE_ALREADY_PAIRED` | Session already has a device |
| 403 | `DEVICE_REVOKED` | Device in revocation list |
| 503 | `NO_ACTIVE_CONTEXT` | Agent has no clinical context yet |
| 400 | `SCHEMA_VALIDATION_FAILED` | Message does not match schema |

---

### 3.6 API Version Negotiation

The QR payload includes `"apiVersion": "1"`. If the phone app receives a 426 response, it must prompt the user to update the app.
