# ADR 0011: DICOM tag allowlist

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Spec sezione 5.2 introduce `GET /api/series/:sid/dicom_meta` con header
DICOM "selezionati" e nota "Allowlist esplicita di tag DICOM esposti.
Mai pass-through completo (rischio leak PHI in tag privati)".

DICOM ha:

- ~3000+ tag standard, molti dei quali hanno PHI (PatientName,
  PatientID, PatientBirthDate, AccessionNumber, ReferringPhysicianName,
  StudyDescription, ecc.).
- Tag privati (0x0000-0xFFFF in gruppi dispari) non standardizzati,
  variabili per vendor, possibile contenitore di workflow PHI.
- Tag derivati (PixelData, ICC profile, Curves, Overlays) che vanno
  letti dal binario, non esposti come metadata.

Senza allowlist, è facile esporre tag con PHI. Con allowlist troppo
stretta, gli agenti perdono context utile (modality, body part,
dosing).

## Decision

**Allowlist esplicita versionata in repo**:

File: `backend/src/bvphoenix/services/dicom_meta_allowlist.py` con
struttura:

```python
DICOM_META_ALLOWLIST_V1: dict[str, dict] = {
    # Modality / acquisition
    "Modality": {"tag": (0x0008, 0x0060), "vr": "CS"},
    "BodyPartExamined": {"tag": (0x0018, 0x0015), "vr": "CS"},
    "ProtocolName": {"tag": (0x0018, 0x1030), "vr": "LO"},
    "AcquisitionDate": {"tag": (0x0008, 0x0022), "vr": "DA"},
    "AcquisitionTime": {"tag": (0x0008, 0x0032), "vr": "TM"},
    "Manufacturer": {"tag": (0x0008, 0x0070), "vr": "LO"},
    "ManufacturerModelName": {"tag": (0x0008, 0x1090), "vr": "LO"},
    "DeviceSerialNumber": {"tag": (0x0018, 0x1000), "vr": "LO"},
    # Geometry
    "SliceThickness": {"tag": (0x0018, 0x0050), "vr": "DS"},
    "PixelSpacing": {"tag": (0x0028, 0x0030), "vr": "DS"},
    "Rows": {"tag": (0x0028, 0x0010), "vr": "US"},
    "Columns": {"tag": (0x0028, 0x0011), "vr": "US"},
    "ImageOrientationPatient": {"tag": (0x0020, 0x0037), "vr": "DS"},
    "ImagePositionPatient": {"tag": (0x0020, 0x0032), "vr": "DS"},
    "PatientPosition": {"tag": (0x0018, 0x5100), "vr": "CS"},
    # CT-specific
    "KVP": {"tag": (0x0018, 0x0060), "vr": "DS"},
    "ContrastBolusAgent": {"tag": (0x0018, 0x0010), "vr": "LO"},
    "SliceLocation": {"tag": (0x0020, 0x1041), "vr": "DS"},
    # PET-specific
    "Units": {"tag": (0x0054, 0x1001), "vr": "CS"},
    "DecayCorrection": {"tag": (0x0054, 0x1102), "vr": "CS"},
    # ... etc
}
```

Vincoli:

- **Niente tag con PHI nominale**: `PatientName, PatientID,
  PatientBirthDate, PatientAddress, ReferringPhysicianName,
  PerformingPhysicianName, OperatorsName, AccessionNumber,
  StudyDescription, SeriesDescription, IssuerOfPatientID`,
  e tutti i private tag.
- **Niente tag dei dati raw**: `PixelData, OverlayData, CurveData,
  ICCProfile`, ecc.
- **Versionata**: la costante ha `_V1` nel nome. Nuove versioni
  aggiungibili con migration semantica (mai breaking).
- **Review trimestrale** della lista. Un nuovo tag entra solo dopo
  conferma "non contiene PHI in nessun vendor".

Endpoint `GET /dicom_meta` ritorna **solo** i tag della allowlist.
Tag presenti in DICOM ma non in allowlist: silenziosamente droppati.

## Consequences

### Positive

- PHI leak via DICOM eliminato al confine API.
- Lista versionata in code-review: ogni cambiamento ha quattro occhi.
- Test deterministico: snapshot fixture con tag DICOM ricco, output
  allowlist verificabile diff-by-diff.
- Documentazione self-referente: chi vuole sapere "che metadata
  espone phoenix ai client?", legge il file.

### Negative

- Manutenzione: ogni vendor nuovo o modality nuova può richiedere
  estensione allowlist. Pull request necessaria.
- Tag esoterici (es. 4D MR particolari) potrebbero non essere
  esposti finché non aggiungi a manualmente.

## Alternatives considered

- **Denylist**: lista PHI-tag da escludere, tutto il resto passa.
  Rifiutato: il rischio di "abbiamo dimenticato di excludere tag X
  che il vendor Y popola con PHI" è non accettabile in clinico.
- **Allowlist da file YAML/JSON**: più facile da editare ma perde
  type-safety Python. Tradeoff non vale.
- **Allowlist letta a runtime da DB**: feature flag-able, ma
  introduce attack surface (admin malicious puede aprire la
  allowlist).

## Implementation hooks

- `services/dicom_meta_allowlist.py`: la costante.
- `services/dicom_meta.py` (Sprint 5): funzione
  `extract_allowlisted(dataset, version="v1")` che ritorna
  dict tag-name -> value, applicando VR-specific normalization.
- `api/studies.py`: endpoint `GET /api/series/:sid/dicom_meta`.
- Test:
  - Snapshot fixture DICOM ricco di PHI tags + allowlist V1 ->
    output non contiene PHI.
  - Tag privato (0x0009, 0x1001) custom vendor -> droppato.
  - Versioning: chiamata con `?version=v1` esplicita ritorna stessa
    cosa di default.

## Riferimenti

- DICOM standard PS3.6 (Data Dictionary):
  https://dicom.nema.org/medical/dicom/current/output/html/part06.html
- TCIA de-identification recommendations (panoramica PHI tags):
  https://wiki.cancerimagingarchive.net/display/Public/De-identification+Knowledge+Base
