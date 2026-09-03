# ADR 0021: Riconciliazione degli inviti per email e identità del contatto

**Status**: Accepted
**Date**: 2026-09-03
**Deciders**: Angelo Leto

## Context

Tre difetti osservati in produzione sul fascicolo di una paziente reale
avevano la stessa radice: l'indirizzo email è la chiave con cui il
sistema collega una persona a un fascicolo, ma nessuno strato la
trattava come un'identità.

1. **Contatti duplicati non eliminabili.** Il form di modifica paziente
   ricostruiva ogni contatto come `{label, relationship, email, phone}`
   scartando l'`id`. `replace_all_contacts` faceva il match sull'`id`,
   quindi ogni salvataggio inseriva una riga nuova per ogni contatto,
   mentre le righe con una delega attiva venivano risparmiate dalla
   cancellazione. Cinque contatti sono diventati otto, e togliere un
   duplicato dallo stesso form ne produceva altri due. In più
   `delete_contact` leggeva i puntatori `delegation_*` senza verificare
   se il grant fosse ancora vivo: un grant revocato per una via diversa
   da `revoke_contact_delegation` lasciava i quattro campi valorizzati,
   e il contatto restava non cancellabile per sempre (409, senza più
   nulla da revocare).

2. **Delega senza riconciliazione.** `_resolve_or_create_grantee`
   cercava un `User` per email; se non esisteva, il grant veniva emesso
   a `PUBLIC_SUBJECT_ID` e il link diventava la credenziale. Se la
   persona si registrava in seguito con lo stesso indirizzo, **nessun
   percorso ricollegava le due cose**: il grant restava su PUBLIC, il
   fascicolo non compariva nel suo account, e l'unica via d'accesso
   restava l'URL originale. Per sempre, e per ogni condivisione futura.

3. **Account creati dal claim, inutilizzabili.** `_perform_claim`
   creava lo `User` con `email_verified_at` NULL e non emetteva alcun
   token di verifica. Con `require_email_verification` attivo in
   produzione, `POST /api/auth/login` rispondeva 403 a ogni tentativo
   successivo e `/api/auth/verify-email` non aveva nulla da consumare:
   account bloccato dalla nascita, con la password giusta. Il claim
   restituisce una sessione, quindi il blocco emergeva solo alla sua
   scadenza, dodici ore dopo, con l'aspetto di una password dimenticata.

Sopra questi tre c'era una confusione di interfaccia: la password del
dialogo di delega finiva in `ShareLink.password_hash` (sblocca il link)
e veniva presentata all'operatore accanto all'URL come se fosse la
password di accesso della persona.

## Decision

### 1. Un invito si attacca a chi ha dimostrato di controllare la casella

Nuovo servizio `services/invitations.py`. Uno `share_link` con
`recipient_email` è un *invito*; il suo grant, finché resta su PUBLIC, è
in attesa. Viene ripuntato sul subject quando quel subject ha
`users.email_verified_at IS NOT NULL` **e nient'altro**.

Il match sul solo `recipient_email` sarebbe una primitiva di
account-takeover: chi scopre che un fascicolo è stato condiviso con
`tizio@example.com` registrerebbe quell'indirizzo e raccoglierebbe il
grant. La verifica chiude il buco perché il token di verifica viene
consegnato solo alla casella.

Chiamato da `POST /auth/verify-email` (il momento in cui la prova si
stabilisce), da `POST /auth/login` (recupera gli account verificati
prima di questa release e gli inviti emessi mentre l'utente era
disconnesso), e da `_perform_claim` / `_perform_bind`.

**Scartato**: canonicalizzare gli indirizzi oltre la forma memorizzata
(punti e `+` alla Gmail). Gmail tratta `a.b@` e `ab@` come una casella
sola, quasi tutti gli altri provider no: normalizzarli attaccherebbe un
invito indirizzato a una persona sull'account di un'altra presso un
provider che li tiene distinti.

**Scartato**: un segreto `inv` nella query string di
`/shared/{token}/info` che marchi l'indirizzo come verificato. Una prova
di identità in una query string finisce nei log di Traefik e di Next,
nella cronologia sincronizzata del browser e nelle schede recenti.

### 2. Tre invarianti nel datastore, non nel codice (alembic 0048)

- `trg_grants_revoked_clear_contact_delegation` /
  `trg_grants_deleted_clear_contact_delegation`: un grant che muore
  azzera i puntatori di delega sul contatto, per qualunque via muoia.
  Lo stato che rendeva un contatto non cancellabile diventa
  irrappresentabile. Il trigger DELETE è BEFORE, non AFTER: l'`ON DELETE
  SET NULL` della FK cancellerebbe altrimenti il `delegation_grant_id`
  su cui il trigger fa il match. E ritorna `OLD`, non `NULL`: un trigger
  BEFORE ROW che ritorna NULL **annulla l'operazione**, e il grant non
  verrebbe mai cancellato.
- `trg_grants_grantee_write_once`: `grants.grantee_subject_id` si può
  spostare solo da PUBLIC. Uno spostamento fra due subject reali è un
  trasferimento di proprietà travestito da UPDATE; vietarlo nel
  datastore fa fallire rumorosamente un difetto futuro invece di
  consegnare un fascicolo alla persona sbagliata.
- `trg_users_email_write`: normalizza l'indirizzo e, se cambia, azzera
  `email_verified_at` bruciando i token pendenti. Normalizzare invece di
  rifiutare è deliberato: ogni lookup nel codice fa già `.lower()` prima
  di interrogare, quindi un account salvato come `A@x.com` non potrebbe
  mai fare login; rifiutare la scrittura renderebbe invece ogni percorso
  di inserimento presente e futuro responsabile di ricordarsene, che è
  esattamente la convenzione da cui nasce l'incoerenza. Normalizzazione e
  reset stanno in **una** funzione perché come due trigger BEFORE ROW
  sulla stessa colonna l'ordine dipenderebbe dai nomi.

### 3. Un indirizzo, un contatto, per paziente (alembic 0049)

`uq_patient_contacts_patient_email`. L'indirizzo di un contatto è
contemporaneamente la chiave con cui la delega risolve l'account, il
bersaglio del dispatcher di notifica e l'identità dietro il token di
opt-out RFC 8058: due righe con la stessa casella rendono ambigue tutte
e tre. La migrazione fonde i duplicati (vince chi ha una delega viva,
altrimenti il più vecchio; i campi non nulli e i consensi si sommano, lo
stato di consegna prende il valore più restrittivo) e poi crea
l'indice.

**Scartato**: unicità su `(patient_id, email, label)`. Elimina i
duplicati esatti ma lascia in piedi l'ambiguità che conta — due contatti
che risolvono allo stesso account.

**Costo accettato**: due contatti dello stesso paziente non possono
condividere una casella. Chi la condivide davvero (una coppia anziana)
si registra con il solo numero di telefono.

### 4. Nessun percorso crea più un account non verificabile

`services/account_provisioning.py` raccoglie i due passi che servono a
un account locale per essere usabile (token di verifica sul ledger di
consegna, righe di consenso obbligatorie). `register`,
`resend-verification` e `_perform_claim` chiamano gli stessi.

### 5. La password del link non è una password di accesso

`autogen_password` passa a `False` di default. Il percorso predefinito è
l'invito via email (`POST /api/share-links/{id}/notify`), che non porta
mai la password. Il banner di esito distingue i due casi reali —
l'indirizzo ha già un account (il fascicolo c'è, si accede come sempre)
oppure no (il link è come se lo crea) — e la password, quando c'è, è
etichettata come password *del link*.

## Consequences

- Un delegato che si registra per conto suo con l'indirizzo invitato
  trova il fascicolo nel proprio account dopo aver confermato l'email.
  Non è più costretto al link.
- Un 403 `email_not_verified` al login diventa una schermata con il
  pulsante per rimandare la conferma.
- `POST /api/admin/users/{subject_id}/verification-email` dà
  all'operatore un percorso reale per sbloccare un account, senza
  scrivere a mano nel database. **Non** marca l'indirizzo come
  verificato: attestare il controllo di una casella altrui non è una
  cosa che un operatore possa fare al posto del titolare.
- `POST /auth/resend-verification` guadagna il rate limit per origine di
  rete che gli mancava: è non autenticato e fa spedire posta a un
  indirizzo scelto dal chiamante.

### Asimmetria dichiarata fra GUI e MCP

La GUI può eliminare un contatto delegato revocando l'accesso nella
stessa operazione (`DELETE .../contacts/{cid}?revoke_delegation=true`).
`remove_patient_contact` via MCP **non** passa quel parametro e si ferma
sul 409 `delegation_active`. È deliberato e coerente con
[ADR 0010](0010-consultation-finalize-gating.md): togliere a una persona
l'accesso a un fascicolo è una decisione umana. L'asimmetria è
registrata qui invece di restare implicita.

### Cosa questa decisione non copre

- **La durata della sessione.** Il cookie vale 12 ore e non esiste
  refresh token, quindi un'app installata sul telefono richiede la
  password ogni giorno. È una decisione di postura di sicurezza a sé,
  non affrontata qui.
- **I contatti restano fuori dal DAG di versioning.** Le scritture su
  `patient_contacts` non passano da `record_versioned_change`, quindi la
  cronologia del fascicolo non le mostra e l'ETag del paziente non si
  muove quando cambia un contatto. Collegarle richiede prima di decidere
  la policy di branch per le scritture dei non-proprietari (oggi
  `resolve_branch_for_write` risponde 403), che è una decisione di
  design separata.
- **Il cambio di indirizzo email da parte dell'utente.** Il trigger
  garantisce che un cambio azzeri la verifica, ma non esiste una
  funzionalità utente per cambiarlo: quando arriverà avrà bisogno del
  proprio flusso di ri-verifica.
