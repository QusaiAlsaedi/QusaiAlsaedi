# Mobile Dictation Agent — HL7 ORU Template Strategy

## 6. HL7 ORU Template Strategy

### 6.1 HL7 Fundamentals for This Use Case

| Field | Meaning |
|---|---|
| `MSH` | Message header — routing, encoding, IDs |
| `PID` | Patient identification — MRN, name, DOB, sex |
| `PV1` | Patient visit — optional, for encounter linkage |
| `ORC` | Common order — order control, status |
| `OBR` | Observation request — accession, modality, procedure |
| `OBX` | Observation/value — report text, status |
| `ZDS` | Z-segment (optional, site-specific) — DICOM Study UID linkage |

**HL7 ORU^R01** is the standard message type for radiology report results.

**OBR-25 (Result Status)** is the primary status indicator:
- `P` = Preliminary (DRAFT in our workflow)
- `F` = Final (FINAL in our workflow)
- `S` = Partial (can be used for CO_READ_PENDING if desired)

---

### 6.2 Template File: `hl7_template.json`

```json
{
  "version": "1.0",
  "encoding": {
    "fieldSeparator":    "|",
    "componentSeparator": "^",
    "repetitionSeparator": "~",
    "escapeChar":         "\\",
    "subcomponentSeparator": "&",
    "segmentTerminator":  "\r"
  },
  "substitutions": {
    "dateFormat":     "yyyyMMdd",
    "datetimeFormat": "yyyyMMddHHmmss",
    "hl7EscapeReportText": true
  },
  "templates": {
    "DRAFT": [
      "MSH|^~\\&|{SENDING_APP}|{SENDING_FACILITY}|{RECEIVING_APP}|{RECEIVING_FACILITY}|{TIMESTAMP}||ORU^R01|{MSG_CTRL_ID}|P|2.5.1|||AL|NE||UNICODE UTF-8|||",
      "PID|1||{MRN}^^^{ASSIGNING_AUTHORITY}^MR||{PATIENT_NAME}||{DOB}|{SEX}|||||||||||",
      "ORC|RE|{ORDER_NUMBER}|{ACC}||CM||||{ORDER_DATETIME}|||{ORDERING_PROVIDER}||||",
      "OBR|1|{ORDER_NUMBER}|{ACC}|{PROC_CODE}^{PROCEDURE}^{PROC_CODING_SYSTEM}|||{ORDER_DATETIME}|||||||{ORDERING_PROVIDER}||{ACC}|||{TIMESTAMP}|||P|||||||{PERFORMING_SITE}",
      "OBX|1|TX|59380-7^Radiology Report^LN||{REPORT_TEXT}||||||P|||{TIMESTAMP}||{PERFORMING_SITE}^{PERFORMING_SITE}^99SITE",
      "OBX|2|ST|59775-8^Status^LN||DRAFT||||||P|||{TIMESTAMP}"
    ],
    "FINAL": [
      "MSH|^~\\&|{SENDING_APP}|{SENDING_FACILITY}|{RECEIVING_APP}|{RECEIVING_FACILITY}|{TIMESTAMP}||ORU^R01|{MSG_CTRL_ID}|P|2.5.1|||AL|NE||UNICODE UTF-8|||",
      "PID|1||{MRN}^^^{ASSIGNING_AUTHORITY}^MR||{PATIENT_NAME}||{DOB}|{SEX}|||||||||||",
      "ORC|RE|{ORDER_NUMBER}|{ACC}||CM||||{ORDER_DATETIME}|||{ORDERING_PROVIDER}||||",
      "OBR|1|{ORDER_NUMBER}|{ACC}|{PROC_CODE}^{PROCEDURE}^{PROC_CODING_SYSTEM}|||{ORDER_DATETIME}|||||||{ORDERING_PROVIDER}||{ACC}|||{TIMESTAMP}|||F|||||||{PERFORMING_SITE}",
      "OBX|1|TX|59380-7^Radiology Report^LN||{REPORT_TEXT}||||||F|||{TIMESTAMP}||{PERFORMING_SITE}^{PERFORMING_SITE}^99SITE",
      "OBX|2|ST|59775-8^Status^LN||FINAL||||||F|||{TIMESTAMP}",
      "OBX|3|ST|18782-3^Approver^LN||{APPROVER_NAME}||||||F|||{TIMESTAMP}"
    ],
    "SECOND_READ": [
      "MSH|^~\\&|{SENDING_APP}|{SENDING_FACILITY}|{RECEIVING_APP}|{RECEIVING_FACILITY}|{TIMESTAMP}||ORU^R01|{MSG_CTRL_ID}|P|2.5.1|||AL|NE||UNICODE UTF-8|||",
      "PID|1||{MRN}^^^{ASSIGNING_AUTHORITY}^MR||{PATIENT_NAME}||{DOB}|{SEX}|||||||||||",
      "ORC|RE|{ORDER_NUMBER}|{ACC}||CM||||{ORDER_DATETIME}|||{ORDERING_PROVIDER}||||",
      "OBR|1|{ORDER_NUMBER}|{ACC}|{PROC_CODE}^{PROCEDURE}^{PROC_CODING_SYSTEM}|||{ORDER_DATETIME}|||||||{ORDERING_PROVIDER}||{ACC}|||{TIMESTAMP}|||S|||||||{PERFORMING_SITE}",
      "OBX|1|TX|59380-7^Radiology Report^LN||{REPORT_TEXT}||||||S|||{TIMESTAMP}||{PERFORMING_SITE}^{PERFORMING_SITE}^99SITE",
      "OBX|2|ST|59775-8^Status^LN||SECOND_READ_PENDING||||||S|||{TIMESTAMP}",
      "OBX|3|ST|18782-3^Co-Reader^LN||{COREADER_NAME}||||||S|||{TIMESTAMP}"
    ]
  }
}
```

---

### 6.3 Example ORU — DRAFT (Preliminary)

```
MSH|^~\&|MDA|RADIOLOGY|RIS|HOSPITAL|20250715143022||ORU^R01|550e8400-e29b-41d4-a716-446655440000|P|2.5.1|||AL|NE||UNICODE UTF-8|||
PID|1||1234567^^^HOSPITAL^MR||DOE^JOHN^MICHAEL||19500101|M|||||||||||
ORC|RE|ORD-2025-001|RAD-2025-456789||CM||||20250715093200|||SMITH^JANE^DR||||
OBR|1|ORD-2025-001|RAD-2025-456789|71250^CT Chest with Contrast^CPT|||20250715093200|||||||SMITH^JANE^DR||RAD-2025-456789|||20250715143022|||P|||||||MAIN RADIOLOGY
OBX|1|TX|59380-7^Radiology Report^LN||CT CHEST WITH CONTRAST\X0D\TECHNIQUE: Axial CT images of the chest were acquired following intravenous contrast administration.\X0D\FINDINGS: The lungs are clear bilaterally. No pleural effusion. Mediastinum unremarkable.\X0D\IMPRESSION: No acute cardiopulmonary disease.||||||P|||20250715143022||MAIN RADIOLOGY^MAIN RADIOLOGY^99SITE
OBX|2|ST|59775-8^Status^LN||DRAFT||||||P|||20250715143022
```

**Notes on the DRAFT example:**
- `OBR-25 = P` (Preliminary)
- `OBX-1` contains the report text; `\X0D\` is the HL7 escape for carriage return (line break within OBX value)
- `OBX-2` carries our application-level status string (`DRAFT`) for downstream systems that look for it
- `MSG_CTRL_ID` is a UUID ensuring uniqueness and deduplication

---

### 6.4 Example ORU — FINAL

```
MSH|^~\&|MDA|RADIOLOGY|RIS|HOSPITAL|20250715143800||ORU^R01|6ba7b810-9dad-11d1-80b4-00c04fd430c8|P|2.5.1|||AL|NE||UNICODE UTF-8|||
PID|1||1234567^^^HOSPITAL^MR||DOE^JOHN^MICHAEL||19500101|M|||||||||||
ORC|RE|ORD-2025-001|RAD-2025-456789||CM||||20250715093200|||SMITH^JANE^DR||||
OBR|1|ORD-2025-001|RAD-2025-456789|71250^CT Chest with Contrast^CPT|||20250715093200|||||||SMITH^JANE^DR||RAD-2025-456789|||20250715143800|||F|||||||MAIN RADIOLOGY
OBX|1|TX|59380-7^Radiology Report^LN||CT CHEST WITH CONTRAST\X0D\TECHNIQUE: Axial CT images of the chest were acquired following intravenous contrast administration.\X0D\FINDINGS: The lungs are clear bilaterally. No pleural effusion. Mediastinum unremarkable.\X0D\IMPRESSION: No acute cardiopulmonary disease.||||||F|||20250715143800||MAIN RADIOLOGY^MAIN RADIOLOGY^99SITE
OBX|2|ST|59775-8^Status^LN||FINAL||||||F|||20250715143800
OBX|3|ST|18782-3^Approver^LN||JOHNSON^MARK^DR||||||F|||20250715143800
```

**Notes on the FINAL example:**
- `OBR-25 = F` (Final)
- `OBX-3` carries the approving radiologist name
- Same `ACC` (accession) as DRAFT — downstream system matches on this to update the record

---

### 6.5 Configurable Fields and Where to Put Status

| Token | Where populated | Configurable? |
|---|---|---|
| `{STATUS}` | OBR-25 (HL7 standard) + OBX-2 value | OBR-25 is standard. OBX-2 is optional per site. |
| `{PROC_CODE}` + `{PROC_CODING_SYSTEM}` | OBR-4 | Yes — CPT, local code, SNOMED. Default: blank code, LN system |
| `{ASSIGNING_AUTHORITY}` | PID-3.4 | Yes — hospital OID or name |
| `{ORDER_NUMBER}` | ORC-2, OBR-2 | From context if available; if not, use accession |
| `{REPORT_TEXT}` | OBX-1 | Agent escapes special HL7 chars automatically |
| OBX LOINC codes | OBX-3 | Yes — configurable per template |
| `{SENDING_APP/FACILITY}` | MSH-3,4 | Yes — from config |
| `{RECEIVING_APP/FACILITY}` | MSH-5,6 | Yes — from config |

**HL7 special character escaping in report text:**

| Character | HL7 Escape |
|---|---|
| `\|` (pipe) | `\F\` |
| `^` | `\S\` |
| `~` | `\R\` |
| `&` | `\T\` |
| `\r` (newline) | `\X0D\` |
| `\n` | `\X0A\` |

---

### 6.6 ACK Parsing

```
MSH|^~\&|RIS|HOSPITAL|MDA|RADIOLOGY|20250715143801||ACK^R01|...
MSA|AA|550e8400-e29b-41d4-a716-446655440000|Message accepted
```

| MSA-1 | Meaning | Agent action |
|---|---|---|
| `AA` | Application Accept | Mark sent, dequeue |
| `CA` | Commit Accept | Mark sent, dequeue |
| `AE` | Application Error | Log NAK text, retry |
| `CE` | Commit Error | Log NAK text, retry |
| `AR` | Application Reject | Log as permanent, move to dead-letter |
| `CR` | Commit Reject | Log as permanent, move to dead-letter |

**For `AR`/`CR`**: Do not retry automatically. Alert IT. Message stays in dead-letter with NAK text preserved (non-PHI portion only — strip patient fields before logging).

---

### 6.7 Template Customization Points (No Code Changes Required)

Sites can customize without recompiling by editing `hl7_template.json`:

1. **Add/remove OBX segments** — e.g., add a DICOM Study UID OBX, or a reading radiologist NPI OBX
2. **Change OBX LOINC codes** — e.g., use site-specific codes instead of `59380-7`
3. **Modify OBR-4** procedure code — match your site's coding system
4. **Add ZDS segment** — for DICOM study linkage (template supports arbitrary extra segments)
5. **Change OBR-25 values** — e.g., use `R` instead of `P` for preliminary if required
6. **PID-3 identifier type code** — change `MR` to site-specific value
7. **Multi-OBX report text** — for very long reports, the builder splits at 65535 chars per OBX
