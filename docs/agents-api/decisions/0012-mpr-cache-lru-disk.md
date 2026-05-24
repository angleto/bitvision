# ADR 0012: MPR cache LRU disk

**Status**: Accepted
**Date**: 2026-04-30
**Deciders**: Angelo Leto

## Context

Spec sezione 5.1 richiede `GET /api/series/:sid/slice/:idx?plane=axial&ww=...&wl=...`
con MPR (multi-planar reconstruction) e windowing custom. Generalizza
`get_series_thumbnail` esistente.

Caratteristiche del workload:

- Volume CT tipico: 320 slice * 512 * 512 * int16 = ~80 MB in memoria.
- Volume MR multi-sequenza: 200-400 MB.
- Generare una slice MPR (es. coronal da volume axial) richiede
  caricamento del volume completo, reslice via SimpleITK, windowing,
  encode JPEG/PNG/WebP.
- L'agente fa N chiamate sequenziali sulla stessa serie (es. "mostra
  slice da 100 a 150 in coronale"): ricomputare il volume da zero per
  ogni slice è uno spreco.
- Cache hit deve essere quasi-immediata (<50ms) per UX accettabile.

## Decision

**Cache su disco (non memoria) con eviction LRU, cap configurabile,
chiave deterministica**.

Schema chiave:

```
key = sha256(f"{series_id}|{idx}|{plane}|{ww}|{wl}|{format}|{max_side}")
path = f"{cache_root}/mpr/{key[:2]}/{key[2:4]}/{key}.{format}"
```

Setup:

- Default cap: **10 GB** per worker, configurabile via
  `BVP_MPR_CACHE_GB`.
- Default cache root: `/var/cache/bvphoenix/mpr/` (volume mount in K8s
  pod, non shared con altri pod, ma persistente attraverso restart).
- Eviction: LRU su `mtime`. Job di pulizia triggera quando totale
  cache > 80% del cap (alert + soft eviction) o > 95% (hard
  eviction sincrona).
- TTL massimo: 30 giorni anche se LRU non scatta. Volume metadata
  che cambia (es. revisione DICOM) invalida la cache via stamp
  `series.last_modified` nella key.

Volume cache (livello sotto):

- I volumi 3D ricostruiti (numpy array) NON sono persistiti su disco;
  vivono in cache memoria (`functools.lru_cache` o `cachetools.LRUCache`)
  per la durata del worker.
- Cap memoria volumi: **2 GB** per worker, configurabile.
- Trade-off: quando il worker restart, perdiamo i volumi caldi e
  dobbiamo ricaricarli dal S3 raw bucket (costo 200-500 ms per volume
  CT). Le slice JPEG su disco rimangono.

## Consequences

### Positive

- Slice cache hit: lettura disco locale + send body, < 30ms p95.
- Chiave deterministica: stessa slice + stessa windowing -> stessa
  cache entry. Niente duplicati per equivalenze (es. preset "abdomen"
  e ww=400/wl=40 collidono se mappati a stessa coppia).
- Cap configurabile per ambiente: CI low (1 GB), prod high (50 GB).
- Cache di slice sopravvive a restart worker.

### Negative

- Disk I/O al posto di RAM: leggermente più lento ma comodo per
  scaling.
- Cache root in volume mount: serve config K8s (PVC) e cleanup al
  pod re-schedule.
- Volume cache memoria perde dati al restart: cold start dolore.

### Mitigazioni

- Pre-warming: al primo accesso a una series, kick off in background
  la generazione delle slice "tipiche" (planes axial+coronal+sagittal,
  preset standard). Sprint 6 stretch.
- Monitoring: metric `mpr_cache_size_bytes`, `mpr_cache_hits_total`,
  `mpr_cache_evictions_total` in `/metrics` Prometheus.

## Alternatives considered

- **Cache in Redis**: shared tra pod, no problemi disk. Ma latency
  network + serializzazione binaria pesante per slice JPEG.
- **No cache**: ogni slice ricomputata. Latency alta, costo CPU
  sproporzionato.
- **Cache in PostgreSQL bytea**: viola separation of concerns,
  pesa il DB.
- **CDN edge**: complicato per signed URL e PHI compliance.

## Implementation hooks

- `services/mpr_cache.py` (Sprint 5): nuova classe `MPRDiskCache`
  con metodi `get(key) -> bytes | None`, `set(key, payload)`,
  `evict_lru()`.
- `services/dicom_mpr.py` (Sprint 5): pipeline reslice, integra
  con cache.
- `api/studies.py`: endpoint `GET /api/series/:sid/slice/:idx`.
- K8s: PVC `mpr-cache-pvc` da 50 GB in prod, mount path
  `/var/cache/bvphoenix/mpr/`.
- Test:
  - Cache miss: prima chiamata genera slice, latency > 200ms.
  - Cache hit: stessa chiamata, latency < 50ms, header `X-Cache: hit`.
  - Eviction: forza riempimento cache > cap, verifica file più
    vecchi rimossi.
  - Volume metadata change: stamp `series.last_modified` cambia ->
    nuova cache key -> nuova entry.

## Open questions

- Pre-warming on ingest? Al momento dell'ingest CT, generare le
  slice axial+coronal+sagittal tutte? Beneficia il primo agente che
  apre il fascicolo, costa CPU all'ingest. Decisione preliminare:
  no pre-warming Sprint 5, valutare in Sprint 6.
