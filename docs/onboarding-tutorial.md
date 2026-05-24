# Tutorial: from first login to first report

This tutorial guides a new user through the complete bitvision phoenix
experience: from initial login to generating the first report and
sharing the Health Record. Designed as an onboarding in about 15
minutes.

## Prerequisites

- Active bitvision phoenix account (verified email). If you do not have one,
  contact the administrator of your instance or sign up at
  `/signup`.
- A CD with a DICOM study (or a folder of extracted DICOM files), or
  a ZIP, a report PDF, or a clinical photo.
- Modern browser (Chrome, Firefox, Safari) with drag-drop enabled.

## Step 1: login

1. Open the URL of your instance (e.g. `https://app.bit.vision`).
2. Enter email and password on the login screen.
3. If you have MFA enabled, enter the OTP code.
4. On success you are redirected to `/dashboard`, where you see recent
   patients, studies in processing, and shortcuts to common actions.

If this is the first login, the interface shows a "Welcome" banner with
a suggestion to create the first patient.

## Step 2: create patient

1. Open `/patients` from the sidebar ("Pazienti" entry).
2. Click **New** at the top right.
3. Fill the form:
   - First name and last name (required)
   - Tax code (optional but recommended, unique)
   - Date of birth, sex (optional)
   - Contacts, allergies, clinical notes (optional)
4. Click **Salva**. You are redirected to `/patients/{id}`, which is
   the patient's empty record page.

## Step 3: upload the CD

The Fascicolo page presents the **Drive UI** (see
[fascicolo-drive-ux.md](./fascicolo-drive-ux.md) for details):

1. From the CD folder on your computer (or from Finder/Explorer),
   drag the entire folder onto the content pane of the Fascicolo.
2. A preview dialog shows the detected content, e.g.:
   ```
   15 file DICOM -> 1 Study (TC torace, 2024-03-12)
   1 DICOMDIR (scartato)
   1 PDF -> Document (classified as lab_result)
   ```
3. Click **Conferma upload**. Files are uploaded with a progress bar.
4. During the upload you can already keep navigating.

## Step 4: wait for processing

After the upload the background worker:

1. Parses the DICOMs, groups them by StudyInstanceUID, creates `Study` and `Series`.
2. Generates thumbnails and key images.
3. Classifies PDFs via rules + LLM (`er_report`, `lab_result`,
   `referral`, ...).
4. Indexes text for full-text search.
5. Computes embeddings (BiomedCLIP for images, text embeddings for PDFs).

The content pane updates via polling / websocket showing the state
("processing", "ready"). Typical time: 30s to 2min for an average study.

## Step 5: view study

1. Click on the study card that just appeared.
2. The DICOM viewer opens in a new page: default hanging protocol,
   wheel scroll to navigate slices, left mouse for
   windowing, double click to zoom.
3. See hotkeys and tools on the [viewer-hotkeys.md](./viewer-hotkeys.md) page.
4. Click **Back** (or breadcrumb) to return to the Fascicolo.

## Step 6: upload a report

1. In the Fascicolo, right-click on the study card.
2. From the context menu choose **Add report**.
3. A dialog opens with two options:
   - Upload existing PDF
   - Write new report (markdown editor)
4. Upload the report PDF. It is auto-classified as `er_report` and
   linked to the study.
5. The report appears as a child item of the study, with a PDF icon.

## Step 7: AI consultation

Two alternative modes:

**A) Claude Desktop / Claude.ai via MCP**

1. Go to `/settings/ai-assistants` and create a new AI assistant.
2. Pick the scopes you want to grant (read families by default; add
   write families for richer flows). The page surfaces a
   reveal-once card with the credentials.
3. Open the patient page and click **Share with AI** to make this
   specific patient visible to the assistant.
4. Follow the [claude-desktop-quickstart.md](./claude-desktop-quickstart.md)
   guide to configure Claude Desktop (stdio) or
   [`agents-api/onboarding-mcp.md`](./agents-api/onboarding-mcp.md)
   for the Claude.ai custom connector (HTTP, ADR 0019).
5. In Claude ask: "Summarize this patient's studies". Claude uses
   the MCP tools exposed by the backend (see
   [agent-protocols.md](./agent-protocols.md)).

**B) Server-side LLM consultation**

1. Go to `/patients/{id}/consultations`.
2. Click **New consultation**.
3. Write the prompt (e.g. "Compare the 2024 CT with the 2023 one").
4. The backend executes the consultation (with access to the entire Health Record) and
   saves the response as a `consultation` in the root folder.

## Step 8: share

1. In the Drive UI, create a new folder: right-click in the content pane →
   **New folder** → name "Pre-op 2024".
2. Drag the study and the report into the folder.
3. Right-click on the "Pre-op 2024" folder → **Share**.
4. Sharing dialog:
   - Type: public link
   - Expiration: 7 days
   - Password (optional)
   - Permissions: read-only
5. Click **Crea link**. Copy the URL and send it to the surgeon.
6. The recipient opens the link, sees the folder with study + report,
   and can open the DICOM viewer without logging in.

See [sharing.md](./sharing.md) and
[shared-link-downloads.md](./shared-link-downloads.md) for details.

## Step 9: monitor

1. In the Fascicolo, switch view: toggle **Timeline** in the header.
2. You see all recent activity in chronological order: uploads, added
   reports, consultations, shares.
3. You can filter by type (studies / reports / documents / annotations).

The timeline is also available via API:
`GET /api/patients/{id}/timeline`.

## Step 10: download Health Record

1. In the Fascicolo page header, click **Esporta fascicolo**.
2. The backend generates a ZIP with:
   - Original DICOMs organized by study
   - Reports and documents in PDF
   - JSON with metadata (demographics, annotations, timeline)
3. Download starts when ready (a few MB to GB depending on the Health Record).

See [patient-fascicolo-export.md](./patient-fascicolo-export.md) for the
exact ZIP format.

## What to do next

Now that you have completed the basic flow, explore:

- [agent-protocols.md](./agent-protocols.md): how to integrate external LLM
  agents (MCP, A2A) with bit.vision.
- [search-and-embeddings.md](./search-and-embeddings.md): how full-text +
  semantic search works.
- [model-registry.md](./model-registry.md): available LLM and imaging
  models, how to switch them.
- [fascicolo-drive-ux.md](./fascicolo-drive-ux.md): detail of the
  Drive paradigm (folders, drag-drop, batch actions).
