"""Delegation pointers, email identity, and the invariants behind invitation reconciliation.

Five structural changes. None of them is visible to the code running at
the moment the migration is applied, which is why they belong to the
*pre-rollout* migration Job (the unique index that IS breaking lives in
0049, applied after the pods carry the new code).

1. ``patient_contacts.email`` is canonicalised by the database.

   The address on a contact is three things at once: the key by which
   ``services.patient_delegation._resolve_or_create_grantee`` finds the
   account a delegation is issued to, the target the notification
   dispatcher sends to, and the identity behind the RFC 8058 opt-out
   token. Two spellings of one mailbox make all three ambiguous. A
   BEFORE trigger lowercases and trims on the way in so no caller can
   opt out, and the existing rows are normalised once.

2. A revoked or deleted grant can no longer leave a live-looking
   delegation pointer on a contact.

   ``patient_contacts.delegation_*`` were only ever cleared by
   ``services.patient_delegation.revoke_contact_delegation``. Every
   other way a grant dies — ``DELETE /api/share-links/{id}`` revoking
   the grant, an admin revocation, an erasure — left the four columns
   pointing at a corpse. ``services.patient_contacts.delete_contact``
   reads those columns to decide whether a contact may be removed, so
   a contact whose delegation had been revoked through any other path
   became permanently undeletable: HTTP 409 forever, with nothing left
   to revoke. Production carried one such row (``cb51bfca…``, pointing
   at grant ``728aa5e8…`` revoked on 2026-05-09).

   The pointers are now cleared by the datastore at the moment the
   grant dies, and the rows already stranded are repaired once. The
   DELETE trigger is BEFORE, not AFTER: the FK's own ON DELETE SET NULL
   would otherwise erase the ``delegation_grant_id`` this trigger
   matches on before it ever ran.

3. ``grants.grantee_subject_id`` becomes write-once off PUBLIC.

   Attaching a pending invitation to the account that proved control of
   the addressed mailbox (``services.invitations``) moves a grant from
   PUBLIC to a real subject. That is the only legitimate move. A move
   from one real subject to another is an ownership transfer wearing an
   UPDATE, and there is no code that should perform it; forbidding it in
   the datastore means a defect in any future caller of that column
   fails loudly instead of silently handing a fascicolo to the wrong
   person.

4. ``users.email`` and ``share_links.recipient_email`` are canonicalised
   by the database, and changing a user's address resets its
   verification state.

   Reconciliation joins one column against the other.
   ``users_email_key`` is a byte-wise unique index, so two rows
   differing only in case could both satisfy a case-insensitive join and
   the sweep would have to choose between two accounts. Normalising on
   write settles it: there is one spelling of each address, and the
   CHECK constraints record what the triggers guarantee.

   Normalising rather than rejecting matters. Every lookup in the
   codebase already lowercases before querying, so an account stored as
   ``A@x.com`` could never be logged into at all; refusing the write
   would instead make every present and future insert path responsible
   for remembering to lowercase, which is precisely the convention that
   let the inconsistency exist.

   The address change and the verification reset live in one trigger
   function on purpose. As two BEFORE ROW triggers on the same column
   they would fire in name order, and whether a case-only edit counted
   as a change — and therefore threw away a valid verification — would
   depend on what the triggers happened to be called. Reconciliation
   trusts ``email_verified_at`` as proof that the account controls the
   address on the row; moving the address without clearing the flag
   turns a proof about the old mailbox into a claim about the new one.
   Outstanding verification tokens are burned in the same statement.

   ``ix_share_links_open_invitation`` is the index the sweep reads.

5. ``email_verification_tokens.user_subject_id`` gains its missing FK.

   The table has always pointed at ``users`` by convention only. The
   trigger in (4) writes to it, and a row whose user is gone is a token
   that can never be used and never be cleaned.

Reversibility: the triggers, functions, index and constraints drop
cleanly. The two backfills (normalised addresses, cleared stale
pointers) are not reversible — the pre-image is not recorded anywhere.

Revision ID: 0048_contact_delegation_and_email_identity
Revises: 0047_clinical_event_amend_time
Create Date: 2026-09-03
"""

from __future__ import annotations

from alembic import op

revision = "0048_contact_delegation_and_email_identity"
down_revision = "0047_clinical_event_amend_time"
branch_labels = None
depends_on = None

# Kept in sync with ``bvphoenix.db.models.sharing.PUBLIC_SUBJECT_ID``.
# Written as a literal because a migration must not import a constant
# that a later release is free to change: the value is part of the
# schema's history, not of today's code.
PUBLIC_SUBJECT_ID = "00000000-0000-0000-0000-000000000001"


_NORMALISE_FN = """
CREATE OR REPLACE FUNCTION public.fn_normalise_email(v text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT nullif(lower(btrim(v)), '') $$;
"""

_CONTACT_EMAIL_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION public.fn_patient_contacts_normalise_email() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            NEW.email := public.fn_normalise_email(NEW.email);
            RETURN NEW;
        END $$;
"""

_CLEAR_DELEGATION_FN = """
CREATE OR REPLACE FUNCTION public.fn_grants_clear_contact_delegation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            gid uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
        BEGIN
            UPDATE patient_contacts
               SET delegation_subject_id    = NULL,
                   delegation_share_link_id = NULL,
                   delegation_grant_id      = NULL,
                   delegation_level         = NULL,
                   updated_at               = now()
             WHERE delegation_grant_id = gid;
            -- OLD, never NULL. This function is attached to a BEFORE
            -- DELETE trigger as well as an AFTER UPDATE one, and a
            -- BEFORE ROW trigger that returns NULL *cancels the
            -- operation* -- the grant would silently never be deleted,
            -- which then breaks every FK cleanup that depends on it.
            -- AFTER triggers ignore the return value, so one branch
            -- serves both.
            RETURN OLD;
        END $$;
"""

_GRANTEE_WRITE_ONCE_FN = f"""
CREATE OR REPLACE FUNCTION public.fn_grants_grantee_write_once() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF OLD.grantee_subject_id IS DISTINCT FROM NEW.grantee_subject_id
               AND OLD.grantee_subject_id <> '{PUBLIC_SUBJECT_ID}'::uuid THEN
                RAISE EXCEPTION
                    'grants.grantee_subject_id is write-once off PUBLIC (grant %); '
                    'revoke the grant and issue a new one instead', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
"""

# Normalisation and the verification reset are ONE function on purpose.
# As two triggers they would both be BEFORE ROW on the same column, and
# PostgreSQL fires those in name order — so whether a case-only edit
# ("A@x" -> "a@x") counted as an address change, and therefore threw
# away a good verification, would depend on how the triggers happened to
# be named. Here the order is written down instead: canonicalise first,
# then compare.
_USER_EMAIL_WRITE_FN = """
CREATE OR REPLACE FUNCTION public.fn_users_email_write() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            NEW.email := public.fn_normalise_email(NEW.email);
            IF NEW.email IS NULL THEN
                RAISE EXCEPTION 'users.email cannot be blank'
                    USING ERRCODE = 'not_null_violation';
            END IF;
            IF TG_OP = 'UPDATE' AND NEW.email IS DISTINCT FROM OLD.email THEN
                -- Reconciliation (services/invitations) treats
                -- email_verified_at as proof that this account controls
                -- the address on this row. Moving the address without
                -- clearing the flag would turn a proof about the old
                -- mailbox into a claim about the new one.
                NEW.email_verified_at := NULL;
                UPDATE email_verification_tokens
                   SET used_at = COALESCE(used_at, now())
                 WHERE user_subject_id = NEW.subject_id AND used_at IS NULL;
            END IF;
            RETURN NEW;
        END $$;
"""

_SHARE_LINK_EMAIL_FN = """
CREATE OR REPLACE FUNCTION public.fn_share_links_normalise_recipient_email() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            NEW.recipient_email := public.fn_normalise_email(NEW.recipient_email);
            RETURN NEW;
        END $$;
"""


def upgrade() -> None:
    op.execute(_NORMALISE_FN)

    # ---- 1. canonical contact addresses -----------------------------
    op.execute(_CONTACT_EMAIL_TRIGGER_FN)
    op.execute(
        "CREATE TRIGGER trg_patient_contacts_normalise_email "
        "BEFORE INSERT OR UPDATE OF email ON patient_contacts "
        "FOR EACH ROW EXECUTE FUNCTION public.fn_patient_contacts_normalise_email()"
    )
    op.execute(
        "UPDATE patient_contacts SET email = public.fn_normalise_email(email) "
        "WHERE email IS DISTINCT FROM public.fn_normalise_email(email)"
    )

    # ---- 2. a dead grant clears its delegation pointers --------------
    op.execute(_CLEAR_DELEGATION_FN)
    op.execute(
        "CREATE TRIGGER trg_grants_revoked_clear_contact_delegation "
        "AFTER UPDATE OF revoked_at ON grants FOR EACH ROW "
        "WHEN (OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL) "
        "EXECUTE FUNCTION public.fn_grants_clear_contact_delegation()"
    )
    op.execute(
        "CREATE TRIGGER trg_grants_deleted_clear_contact_delegation "
        "BEFORE DELETE ON grants FOR EACH ROW "
        "EXECUTE FUNCTION public.fn_grants_clear_contact_delegation()"
    )
    # Repair the rows already stranded by a revocation that took a path
    # other than ``revoke_contact_delegation``.
    op.execute(
        "UPDATE patient_contacts c "
        "   SET delegation_subject_id = NULL, delegation_share_link_id = NULL, "
        "       delegation_grant_id = NULL, delegation_level = NULL, updated_at = now() "
        "  FROM grants g "
        " WHERE g.id = c.delegation_grant_id AND g.revoked_at IS NOT NULL"
    )
    # ...and the rows left half-pointing by a grant that was deleted
    # outright (the FK's ON DELETE SET NULL cleared two of four columns).
    op.execute(
        "UPDATE patient_contacts "
        "   SET delegation_subject_id = NULL, delegation_level = NULL, updated_at = now() "
        " WHERE delegation_grant_id IS NULL AND delegation_share_link_id IS NULL "
        "   AND (delegation_subject_id IS NOT NULL OR delegation_level IS NOT NULL)"
    )

    # ---- 3. grantee is write-once off PUBLIC -------------------------
    op.execute(_GRANTEE_WRITE_ONCE_FN)
    op.execute(
        "CREATE TRIGGER trg_grants_grantee_write_once "
        "BEFORE UPDATE OF grantee_subject_id ON grants "
        "FOR EACH ROW EXECUTE FUNCTION public.fn_grants_grantee_write_once()"
    )

    # ---- 4+5. canonical addresses on both sides of the join, and an
    # address change that invalidates its own verification -------------
    #
    # Normalising rather than refusing is deliberate. Every lookup in the
    # codebase already lowercases before it queries (``api/auth`` login,
    # register and forgot-password all do), so an account stored as
    # ``A@x.com`` could never be logged into: rejecting the write would
    # make each present and future insert path responsible for
    # remembering, which is how the inconsistency arose in the first
    # place. The CHECK stays as the backstop that says what the trigger
    # guarantees.
    op.execute(_USER_EMAIL_WRITE_FN)
    op.execute(
        "CREATE TRIGGER trg_users_email_write "
        "BEFORE INSERT OR UPDATE OF email ON users "
        "FOR EACH ROW EXECUTE FUNCTION public.fn_users_email_write()"
    )
    op.execute(
        "UPDATE users SET email = public.fn_normalise_email(email) "
        "WHERE email IS DISTINCT FROM public.fn_normalise_email(email)"
    )
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT ck_users_email_canonical "
        "CHECK (email = lower(btrim(email)))"
    )

    op.execute(_SHARE_LINK_EMAIL_FN)
    op.execute(
        "CREATE TRIGGER trg_share_links_normalise_recipient_email "
        "BEFORE INSERT OR UPDATE OF recipient_email ON share_links "
        "FOR EACH ROW EXECUTE FUNCTION public.fn_share_links_normalise_recipient_email()"
    )
    op.execute(
        "UPDATE share_links SET recipient_email = public.fn_normalise_email(recipient_email) "
        "WHERE recipient_email IS DISTINCT FROM public.fn_normalise_email(recipient_email)"
    )
    op.execute(
        "ALTER TABLE share_links ADD CONSTRAINT ck_share_links_recipient_email_canonical "
        "CHECK (recipient_email IS NULL OR recipient_email = lower(btrim(recipient_email)))"
    )
    op.execute(
        "CREATE INDEX ix_share_links_open_invitation ON share_links (recipient_email) "
        "WHERE recipient_email IS NOT NULL AND claimed_at IS NULL"
    )

    # ---- 6. the verification token's missing FK ----------------------
    op.execute(
        "DELETE FROM email_verification_tokens t "
        " WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.subject_id = t.user_subject_id)"
    )
    op.execute(
        "ALTER TABLE email_verification_tokens "
        "ADD CONSTRAINT fk_email_verification_tokens_user "
        "FOREIGN KEY (user_subject_id) REFERENCES users(subject_id) ON DELETE CASCADE"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE email_verification_tokens "
        "DROP CONSTRAINT IF EXISTS fk_email_verification_tokens_user"
    )
    op.execute("DROP INDEX IF EXISTS ix_share_links_open_invitation")
    op.execute(
        "ALTER TABLE share_links DROP CONSTRAINT IF EXISTS ck_share_links_recipient_email_canonical"
    )
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_email_canonical")
    op.execute("DROP TRIGGER IF EXISTS trg_share_links_normalise_recipient_email ON share_links")
    op.execute("DROP FUNCTION IF EXISTS public.fn_share_links_normalise_recipient_email()")
    op.execute("DROP TRIGGER IF EXISTS trg_users_email_write ON users")
    op.execute("DROP FUNCTION IF EXISTS public.fn_users_email_write()")
    op.execute("DROP TRIGGER IF EXISTS trg_grants_grantee_write_once ON grants")
    op.execute("DROP FUNCTION IF EXISTS public.fn_grants_grantee_write_once()")
    op.execute("DROP TRIGGER IF EXISTS trg_grants_deleted_clear_contact_delegation ON grants")
    op.execute("DROP TRIGGER IF EXISTS trg_grants_revoked_clear_contact_delegation ON grants")
    op.execute("DROP FUNCTION IF EXISTS public.fn_grants_clear_contact_delegation()")
    op.execute("DROP TRIGGER IF EXISTS trg_patient_contacts_normalise_email ON patient_contacts")
    op.execute("DROP FUNCTION IF EXISTS public.fn_patient_contacts_normalise_email()")
    op.execute("DROP FUNCTION IF EXISTS public.fn_normalise_email(text)")
