# Revisione viewer DICOM — verso "best-in-class"

Data: 2026-05-29. Metodo: review multi-agente (11 mappe di sottosistema, 8 benchmark vs OHIF/Cornerstone3D, RadiAnt, OsiriX/Horos, Weasis, Sectra/Visage/syngo.via, 3 lenti di gap-analysis, 19 verifier avversariali sul codice reale). 92 gap candidati, **0 refutati** (85 confermati, 7 parziali) con evidenza file:line. Stack: Next.js 15 / React 19 + Cornerstone3D v4 + vtk.js.

Distribuzione severità: 9 critical, 48 high, 32 medium, 3 low.

---

## 1. Verdetto

Il viewer non è un prototipo: è una base matura con scelte architetturali di qualità non comune in un viewer web OSS (isolamento PHI, quantificazione PET/SUV, gestione GPU, fix documentati di race su fusion). Ma **oggi non è usabile come reading-tool diagnostico primario**, per tre ragioni che sono indipendenti dalla quantità di feature 3D già presenti:

1. **Sicurezza diagnostica**: non mostra mai chi è il paziente, né l'orientamento anatomico, e la geometria DICOM reale viene scartata. Sono i controlli base che ogni PACS dà per scontati.
2. **Le modalità più lette (CR/DX/MG/US) sono di seconda classe**: rese come JPEG 8-bit server-side, senza strumenti, zoom o laterality.
3. **Il confronto con i priors** (il task di lettura più ricorrente e più "sticky") è di fatto assente e l'unico sync esistente è anatomicamente fuorviante.

La buona notizia: le tre aree poggiano in parte su **un unico keystone architetturale**, e una volta risolto quello, gran parte del resto sono feature incrementali su una base sana.

---

## 2. Il keystone architetturale: geometria DICOM scartata

`volume.raw` (transport, `src/lib/api/index.ts:2656` `PackedVolumeHeader`) porta solo `nx,ny,nz`, `spacing`, `valueRange`. Non porta `origin`, `direction`, `ImageOrientationPatient`, `ImagePositionPatient`, `FrameOfReferenceUID`.

Di conseguenza `makeLocalVolume` (`src/components/CornerstoneMPRLayout.tsx:215-241`) **fabbrica** un frame identità:
```
ImagePositionPatient: [0, 0, 0]
ImageOrientationPatient: [1, 0, 0, 0, 1, 0]
direction: [1,0,0, 0,1,0, 0,0,1]
FrameOfReferenceUID: sintetico, forzato condiviso (riga 646)
```

La geometria reale **esiste già** lato backend (l'oggetto Series espone `FrameOfReferenceUID` e `ImageOrientationPatient`, `src/lib/api/index.ts:290-300`): viene solo scartata al build del volume.

Questa singola scelta è la radice di quattro gap distinti:
- gli orientation markers L/R/A/P sarebbero un'assunzione, non un dato (acquisizioni feet-first/oblique mislabeled);
- il `FrameOfReferenceUID` sintetico condiviso **disabilita il controllo di FoR-mismatch di Cornerstone** (`CornerstoneMPRLayout.tsx`, gap safety/medium);
- le misure in "spazio mondo" vivono in un frame sintetico (lunghezze in mm restano corrette se spacing e ortogonalità reggono, ma labeling assiale e geometria obliqua no);
- il sync spaziale tra studi (priors) è impossibile senza FoR/coordinate-mondo reali.

**Fix proper (unica opzione corretta)**: estendere `PackedVolumeHeader` con `origin` (IPP del primo voxel), `direction` (3x3 da IOP + coseno di slice), `frameOfReferenceUID`; thread-arli in `makeLocalVolume`. Modifica backend piccola (i dati sono nel DICOM). Sblocca orientation markers, labeling corretto, FoR-safety, e il sync dei priors.

---

## 3. Punti di forza (già a livello o vicini a best-in-class)

Da preservare in qualsiasi refactor:

- **Isolamento PHI architetturale**: `purgeCornerstoneCache()` su ogni cambio `params.id` (rilascia volumi + texture GPU), `volumeId` namespaced per `seriesId`, nessun bucket/URL/presigned sul wire (read binarie via endpoint opachi). Coerente in tutti i sottosistemi.
- **Quantificazione PET/SUV** insolitamente profonda per un viewer web: 6 varianti di normalizzazione SUV, armonizzazione EANM/EARL, floor PERCIST 1.0 per lesione misurabile; ROI-stats calcolate **server-side** (kernel 1cm³) anziché fidarsi del sampling client.
- **Misure in spazio fisico (world mm)** via tool nativi Cornerstone3D, non in pixel-schermo.
- **Fusion hardening**: `resolveFusionOrder` puro e unit-tested, gate documentato contro la race del doppio `setVolumes`, watchdog 30s, fail-flag. Cross-volume crosshair jump (IJK→world→IJK) corretto per il caso PT-su-CT.
- **Disciplina API Cornerstone 4.x**: VOI/colormap/opacity via `setProperties(props, volumeId)` (non il path rotto `getActor`), sort monotono degli stop OTF/CTF prima di `addRGBPoint`, MONOCHROME1 gestito con XOR.
- **Hanging/hotkeys/state**: moduli separati e puri, restore type-guarded, gate save-after-load, listener attaccato una sola volta via ref, guard sui typing-target.
- **Cancellation discipline** nelle route multi/compare (AbortController, flag cancelled), failure isolation per-pane, wheel non-passive corretto in tutte le superfici di scrub.
- **Integrazione referto**: il composer riusa il modello Consultation (eredita versioning/sign-off/audit F12), le citazioni dei marker rendono i referti referenziali (click → riapre il viewer alla slice).

---

## 4. Roadmap prioritizzato per tema

Severità tra parentesi. Effort: S (<0.5g), M (1-2g), L (3-5g), XL (>1 settimana).

### A. Sicurezza diagnostica — BLOCCANTE per uso primario
| Gap | Sev | Effort | File |
|---|---|---|---|
| Geometria DICOM reale (keystone §2): thread IPP/IOP/FoR | critical | L | CornerstoneMPRLayout.tsx, api/index.ts |
| Banner identità paziente/studio sempre visibile + guard correct-patient su compare | critical | M | page.tsx, CornerstoneMPRLayout.tsx |
| Orientation markers L/R/A/P/H/F su ogni pane (da camera, ricalcolati su CAMERA_MODIFIED) | critical | M | CornerstoneMPRLayout.tsx, Series2DViewer.tsx, VolumeViewer.tsx |
| Indicatore FLIPPED/ROTATED/INVERT sui pane trasformati | high | M | CornerstoneMPRLayout.tsx |
| FoR sintetico condiviso disabilita il check di FoR-mismatch di CS | medium | M | CornerstoneMPRLayout.tsx |
| Warning misura non calibrata quando manca PixelSpacing | high | M | CornerstoneMPRLayout.tsx |
| Undo/redo + restore misure (clear-all e delete sono irreversibili) | high | M | page.tsx, CornerstoneMPRLayout.tsx |
| signReport forka una seconda Consultation: orphan draft editabile dopo firma | high | S | ReportComposer.tsx |
| ROI SUV on-canvas usa sempre variante 'bw' ignorando la selezione | high | S | CornerstoneMPRLayout.tsx |
| Preset PT SUV mostrati anche su PET non SUV-scaled | medium | S | WLPresetBar.tsx |
| Nessuna advisory uncalibrated-display / postura GSDF | medium | S | page.tsx |

### B. Path 2D full-fidelity (CR/DX/MG/US) — BLOCCANTE per le modalità più lette
| Gap | Sev | Effort | File |
|---|---|---|---|
| Single-frame/2D via Cornerstone StackViewport (full bit-depth, tool group completo, W/L come LUT GPU, laterality/scale) | critical | XL | Series2DViewer.tsx, page.tsx |
| BrowserSupportGate blocca anche il fallback 2D che non usa WebGL2 | high | S | BrowserSupportGate.tsx, page.tsx |
| 2D round-trip JPEG 2048px al server per ogni drag W/L e slice-step | high | L | Series2DViewer.tsx |
| Cine per multi-frame 2D reali (XA/fluoro, US/echo, multi-frame MG) | high | L | Series2DViewer.tsx, page.tsx |

### C. Performance / loading
| Gap | Sev | Effort | File |
|---|---|---|---|
| Progressive/streaming load: niente schermo bianco fino al download completo (HTJ2K o slice-streaming) | critical | XL | page.tsx, api/index.ts, CornerstoneMPRLayout.tsx |
| Wire Float32 (4 byte/voxel, ~2x int16, gzip-resistant) → int16 | high | L | api/index.ts |
| Nessun budget cache Cornerstone (`setMaxCacheSize`); compare/multi possono OOM / perdere il contesto WebGL (fino a 12 contesti) | high | M | cornerstoneSetup.ts, page.tsx |
| Download volume non cancellato su cambio-serie/unmount | high | S | page.tsx |
| Oblique reslice: resample trilineare full sul main thread per frame | medium | L | ObliqueMPRViewport.tsx |
| Marching cubes STL sincrono main-thread, output solo ASCII | high | L | isosurfaceSTL.ts |
| CAMERA_MODIFIED doppio-sottoscritto, lavoro per-frame non throttled | medium | M | CornerstoneMPRLayout.tsx |
| Polling reconcile marker 15s (study+series) invece di SSE | low | XL | (marker sync) |

### D. Confronto con priors — BLOCCANTE per follow-up oncologico/post-op
| Gap | Sev | Effort | File |
|---|---|---|---|
| Prior browser patient-scoped (ordinato per data, stessa modalità/distretto) + "compare with prior" one-click | critical | XL | SeriesPicker.tsx, compare/page.tsx, multi/page.tsx |
| Sync world-coordinate (`createCameraPositionSynchronizer` + `createVOISynchronizer` su FoR); oggi sync per delta di indice slice, anatomicamente errato | high | L | ComparisonLayout.tsx, page.tsx |
| Route /viewer/compare (l'unica sync-capable) irraggiungibile dalla UI | high | M | StudyDetailContent.tsx, multi/page.tsx |

### E. Strumenti di refertazione
| Gap | Sev | Effort | File |
|---|---|---|---|
| Reference/localizer lines (`ReferenceLinesTool`) tra pane MPR e tra serie | high | M | CornerstoneMPRLayout.tsx, cornerstoneSetup.ts |
| Thick-slab MIP/MinIP/AvgIP con slider spessore sui pane MPR | high | M | CornerstoneMPRLayout.tsx |
| Bidirectional (RECIST) registrato ma irraggiungibile, risultato appiattito a length | high | S | page.tsx |
| Misure persistite non ridisegnate come annotation editabili dopo reload | high | L | page.tsx |
| Id misura per array-index numerico clobbera i marker server-seed (i cancellati riappaiono) | high | M | CornerstoneMPRLayout.tsx |
| Key-image / Secondary Capture / KOS verso il referto | medium | L | CornerstoneMPRLayout.tsx, page.tsx |
| Inserimento misure nel referto = dump markdown flat, senza struttura né binding live | high | L | ReportComposer.tsx |
| Scale bar / righello calibrato on-image (MPR/3D/2D) | high | S | CornerstoneMPRLayout.tsx |
| Magnifier/loupe e HUD hover-value | low | S | — |

### F. Segmentation
| Gap | Sev | Effort | File |
|---|---|---|---|
| SEG/labelmap invisibile e non editabile sui pane 2D/MPR (API segmentation Cornerstone) | high | XL/L | CornerstoneMPRLayout.tsx |
| Threshold seg + conversione mask: loop full-volume sul main thread | medium | M | SegmentationControls.tsx |
| Auto-seg: poll indovina il completamento dal listing label e leaka su unmount | medium | M | SegmentationImporter.tsx |
| `fusionAlpha` dead control: dichiarato e mostrato ma mai applicato all'opacità | medium | S | CornerstoneMPRLayout.tsx |

### G. Windowing / DICOM presentation
| Gap | Sev | Effort | File |
|---|---|---|---|
| Nessun supporto VOI LUT non-lineare (SIGMOID): `VOILUTFunction` hardcoded LINEAR | medium | L | CornerstoneMPRLayout.tsx |
| Auto-WL implementato due volte con algoritmi divergenti | medium | M | windowing.ts |
| W/L + invert non persistiti tra reload/sessione | high | M | page.tsx |
| 4D/dynamic PET ridotto a un singolo time frame: no scrubber temporale né TAC | medium | L | page.tsx |

### H. UX / muscle-memory / state
| Gap | Sev | Effort | File |
|---|---|---|---|
| Default left-drag = Crosshairs invece di W/L; W/L su slider sidebar; no preset su number-key (diverge da muscle-memory PACS) | medium/high | L | CornerstoneMPRLayout.tsx |
| Hotkey/mouse-binding non rimappabili | medium | L | hotkeys.ts |
| Layout/MIP/oblique/protocollo non persistiti; restore va in race col re-pick del protocollo | high | M | page.tsx |
| Slice position mostrata come voxel-index 1-based, non slice-location DICOM in mm | high | L | CornerstoneMPRLayout.tsx |
| `reorderForPrimary` calcolato ma scartato: l'acquisition plane non diventa il pane primario; 2x3/3x3 rendono celle vuote | high/medium | M | page.tsx |
| Keyboard slice-step e cine hardcoded sull'asse Z assiale a prescindere dal pane attivo | high | M | page.tsx |
| Corner-overlay DICOM canonico (studio/serie/window/zoom/tecnica) come HUD PACS | high | M | CornerstoneMPRLayout.tsx |
| Right-click soppresso ma non rimpiazzato | medium | M | CornerstoneMPRLayout.tsx |

### I. i18n / accessibilità
| Gap | Sev | Effort | File |
|---|---|---|---|
| Chrome a locale misto: header IT/EN affiancati, pannelli segmentation/compare/2D hardcoded English | high/medium | M-L | page.tsx, SegmentationControls.tsx |
| Hotkey overlay, label hanging-protocol e preset W/L non i18n | medium | M | HotkeyHelpOverlay.tsx |

### J. Code-quality / debito
| Gap | Sev | Effort | File |
|---|---|---|---|
| God components da ~4000 righe (page.tsx, CornerstoneMPRLayout.tsx) con `as unknown as` pervasivi attorno al rendering | high | XL | page.tsx, CornerstoneMPRLayout.tsx |
| Due sottosistemi misure coesistono: quello orfano usa math in pixel-schermo | medium | M | measurements.ts |
| Tre decoder blob near-duplicate / path di load Cornerstone paralleli | medium | M | page.tsx |

---

## 5. Le 10 mosse a maggior leva (sequenziate)

L'ordine rispetta le dipendenze (la #2 sblocca #3, #6) e mette la safety prima di tutto.

1. **Banner identità paziente/studio + guard correct-patient** (safety, M). Il #1 controllo wrong-patient: senza, non si può usare in primaria.
2. **Threading geometria DICOM reale — keystone §2** (safety+conformance, L). Sblocca 3, 6 e le misure corrette.
3. **Orientation markers + indicatore flip/rotate/invert** (safety, M; dipende da 2).
4. **Path 2D full-fidelity via StackViewport** (feature, XL) + togliere il blocco WebGL2 sul fallback (S).
5. **Progressive/streaming loading** (perf, XL) + Float32→int16 sul wire (L). È la metrica di velocità percepita #1.
6. **Prior comparison: prior browser patient-scoped + synchronizer world-coord** (workflow, XL; dipende da 2).
7. **Reference/localizer lines + thick-slab MIP/MinIP/AvgIP** (feature, M+M).
8. **Persistenza misure editabili + undo/restore + RECIST bidirectional reale** (bug+feature, L).
9. **Key-image / Secondary Capture → referto con binding** (workflow, L).
10. **Cache budget + cancellation + governance contesti WebGL** (perf, M+S). Stabilità su compare/multi.

---

## 6. Quick wins (effort S, impatto alto) — candidabili subito

- BrowserSupportGate: non bloccare il fallback 2D che non usa WebGL2.
- Cancellare il download del volume su unmount/cambio-serie (bug).
- Persistere W/L + invert tra reload.
- Scale bar on-image.
- Fix SUV variant: usare la variante selezionata, non hardcoded 'bw' (bug).
- Gating preset PT SUV solo su PET SUV-scaled.
- Oblique slab-scroll: clamp dei bounds (il piano può uscire dal dataset, bug).
- signReport: non lasciare l'orphan draft dopo la firma (safety bug).
- `fusionAlpha`: applicarlo davvero all'opacità o rimuoverlo (dead control).

---

## Nota metodologica

I 92 gap sono stati verificati avversarialmente contro il codice (default scettico, evidenza file:line). 0 refutati significa che i mapper non hanno inventato feature mancanti; alcune voci sono near-duplicate tra le 3 lenti (es. orientation markers, thick-slab MIP, sync priors) e qui sono consolidate. Le stime di effort sono indicative del lavoro reale (file/righe/migration), non gonfiate.
