"""One contact per mailbox per patient: merge the duplicates, then forbid them.

This migration is BREAKING for the code that precedes it and belongs to
the *post-rollout* Job. ``services.patient_contacts.replace_all_contacts``
as shipped before this release inserts every incoming entry whose ``id``
does not match an existing row *before* deleting the rows that fell out
of the array, so a ``PATCH /api/patients/{id}`` carrying a contacts array
would hit the unique index mid-statement and 500 for as long as an old
pod is still serving. Apply 0048 before the rollout, this one after.

Why the constraint exists
-------------------------

A contact's address is simultaneously the key
``services.patient_delegation._resolve_or_create_grantee`` uses to find
the account a delegation is issued to, the target the notification
dispatcher sends to, and the identity behind the RFC 8058 opt-out token.
Two rows carrying one mailbox make all three ambiguous: two delegations
resolve to the same account, two dispatch rows race for the same inbox,
and one of the two opt-out tokens silently stops representing anybody.

The duplicates were not a data-entry accident. The patient edit form
rebuilt each contact as ``{label, relationship, email, phone}`` and
dropped the ``id``; ``replace_all_contacts`` matches on ``id``, so every
save inserted a fresh row for every contact, while refusing to delete
the pre-existing rows that carried delegation pointers. Removing a
duplicate through that form and saving therefore produced *more*
duplicates. The client is fixed in the same release; this index is what
makes the class of defect unrepresentable rather than merely absent.

The merge
---------

Within a ``(patient_id, email)`` group the winner is the row carrying a
live delegation, and failing that the oldest row — the one whose id the
outside world has had longest, and the one delegation pointers, tasks
and dispatch history are most likely to reference. Every non-null field
the winner lacks is taken from the losers; consent booleans are OR-ed
(a consent given once is not withdrawn by a merge); the delivery state
takes the most restrictive value present (a bounce or an unsubscribe
must survive); tasks assigned to a loser are repointed at the winner.

Two guards abort the migration rather than lose something that cannot
be reconstructed:

* a group holding two *live* delegations has no defensible winner — the
  operator must revoke one through
  ``DELETE /api/patients/{id}/contacts/{cid}/delegate`` and re-run;
* a loser with rows in ``notification_dispatches`` has had its opt-out
  token mailed out. ``patient_contacts.opt_out_token`` is globally
  unique, so it cannot be carried to the winner, and deleting the row
  turns a live unsubscribe link into a 404 — a GDPR art. 21 defect.
  ``notification_dispatches.contact_id`` is ON DELETE CASCADE, so the
  dispatch history would go with it.

Both guards were verified to hold on production before this was
written: zero groups with two live delegations, and
``notification_dispatches`` empty.

Reversibility: dropping the index restores the previous constraint
surface, but the merge is not reversible — the losers are gone and the
fields folded into the winner are not recorded separately. Snapshot the
database before applying.

Revision ID: 0049_patient_contact_email_unique
Revises: 0048_contact_delegation_and_email_identity
Create Date: 2026-09-03
"""

from __future__ import annotations

from alembic import op

revision = "0049_patient_contact_email_unique"
down_revision = "0048_contact_delegation_and_email_identity"
branch_labels = None
depends_on = None


# ``email_delivery_state`` is ranked inline (rather than through a
# helper function) so this migration leaves nothing behind in any
# schema. The highest rank wins the merge: the state that stops the
# dispatcher must survive it, so a consent withdrawal or a hard bounce
# outranks ``active`` — the only value any other may overwrite.
_MERGE = """
DO $$
DECLARE
    g          record;
    winner_id  uuid;
    loser_ids  uuid[];
    n_live     int;
    n_dispatch int;
BEGIN
    FOR g IN
        SELECT patient_id, email
          FROM patient_contacts
         WHERE email IS NOT NULL
         GROUP BY patient_id, email
        HAVING count(*) > 1
    LOOP
        SELECT count(*) INTO n_live
          FROM patient_contacts c
          JOIN grants gr ON gr.id = c.delegation_grant_id
         WHERE c.patient_id = g.patient_id AND c.email = g.email
           AND gr.revoked_at IS NULL
           AND (gr.valid_until IS NULL OR gr.valid_until > now());
        IF n_live > 1 THEN
            RAISE EXCEPTION
                'patient % has % live delegations on address %; revoke all but one via '
                'DELETE /api/patients/{id}/contacts/{cid}/delegate and re-run the migration',
                g.patient_id, n_live, g.email
                USING ERRCODE = 'data_exception';
        END IF;

        -- Winner: the live delegation if there is one, else the oldest row.
        SELECT c.id INTO winner_id
          FROM patient_contacts c
          LEFT JOIN grants gr ON gr.id = c.delegation_grant_id
                             AND gr.revoked_at IS NULL
                             AND (gr.valid_until IS NULL OR gr.valid_until > now())
         WHERE c.patient_id = g.patient_id AND c.email = g.email
         ORDER BY (gr.id IS NOT NULL) DESC, c.created_at ASC, c.id ASC
         LIMIT 1;

        SELECT array_agg(c.id) INTO loser_ids
          FROM patient_contacts c
         WHERE c.patient_id = g.patient_id AND c.email = g.email AND c.id <> winner_id;

        SELECT count(*) INTO n_dispatch
          FROM notification_dispatches d WHERE d.contact_id = ANY(loser_ids);
        IF n_dispatch > 0 THEN
            RAISE EXCEPTION
                'contacts %  on patient % have % dispatched notifications; their opt-out '
                'tokens are in the wild and cannot be merged away. Resolve by hand.',
                loser_ids, g.patient_id, n_dispatch
                USING ERRCODE = 'data_exception';
        END IF;

        -- Fold every non-null field the winner lacks, OR the consents,
        -- and take the most restrictive delivery state in the group.
        UPDATE patient_contacts w SET
            label              = COALESCE(nullif(btrim(w.label), ''), m.label),
            relationship       = COALESCE(w.relationship, m.relationship),
            phone              = COALESCE(w.phone, m.phone),
            notes              = COALESCE(w.notes, m.notes),
            telegram_chat_id   = COALESCE(w.telegram_chat_id, m.telegram_chat_id),
            whatsapp_phone     = COALESCE(w.whatsapp_phone, m.whatsapp_phone),
            webhook_url        = COALESCE(w.webhook_url, m.webhook_url),
            webhook_secret_encrypted =
                COALESCE(w.webhook_secret_encrypted, m.webhook_secret_encrypted),
            consent_to_contact = w.consent_to_contact OR m.consent_to_contact,
            consent_email      = w.consent_email      OR m.consent_email,
            consent_telegram   = w.consent_telegram   OR m.consent_telegram,
            consent_whatsapp   = w.consent_whatsapp   OR m.consent_whatsapp,
            consent_webhook    = w.consent_webhook    OR m.consent_webhook,
            email_delivery_state = CASE
                WHEN (CASE m.email_delivery_state WHEN 'suppressed' THEN 3
                           WHEN 'bounced' THEN 2 WHEN 'unsubscribed' THEN 1 ELSE 0 END)
                   > (CASE w.email_delivery_state WHEN 'suppressed' THEN 3
                           WHEN 'bounced' THEN 2 WHEN 'unsubscribed' THEN 1 ELSE 0 END)
                THEN m.email_delivery_state ELSE w.email_delivery_state END,
            updated_at         = now()
          FROM (
            SELECT
                (array_agg(label      ORDER BY created_at) FILTER (WHERE btrim(label) <> ''))[1]
                                                                    AS label,
                (array_agg(relationship ORDER BY created_at) FILTER (WHERE relationship IS NOT NULL))[1]
                                                                    AS relationship,
                (array_agg(phone      ORDER BY created_at) FILTER (WHERE phone IS NOT NULL))[1]
                                                                    AS phone,
                (array_agg(notes      ORDER BY created_at) FILTER (WHERE notes IS NOT NULL))[1]
                                                                    AS notes,
                (array_agg(telegram_chat_id ORDER BY created_at)
                    FILTER (WHERE telegram_chat_id IS NOT NULL))[1]  AS telegram_chat_id,
                (array_agg(whatsapp_phone ORDER BY created_at)
                    FILTER (WHERE whatsapp_phone IS NOT NULL))[1]    AS whatsapp_phone,
                (array_agg(webhook_url ORDER BY created_at)
                    FILTER (WHERE webhook_url IS NOT NULL))[1]       AS webhook_url,
                (array_agg(webhook_secret_encrypted ORDER BY created_at)
                    FILTER (WHERE webhook_secret_encrypted IS NOT NULL))[1]
                                                                    AS webhook_secret_encrypted,
                bool_or(consent_to_contact) AS consent_to_contact,
                bool_or(consent_email)      AS consent_email,
                bool_or(consent_telegram)   AS consent_telegram,
                bool_or(consent_whatsapp)   AS consent_whatsapp,
                bool_or(consent_webhook)    AS consent_webhook,
                (array_agg(email_delivery_state ORDER BY
                    (CASE email_delivery_state WHEN 'suppressed' THEN 3
                          WHEN 'bounced' THEN 2 WHEN 'unsubscribed' THEN 1 ELSE 0 END) DESC))[1]
                                                                    AS email_delivery_state
              FROM patient_contacts WHERE id = ANY(loser_ids)
          ) m
         WHERE w.id = winner_id;

        -- Tasks pointed at a loser follow the surviving identity rather
        -- than becoming unassigned via ON DELETE SET NULL.
        UPDATE patient_tasks SET assigned_to_contact_id = winner_id
         WHERE assigned_to_contact_id = ANY(loser_ids);

        -- ``is_primary`` is carried over only after the losers are gone:
        -- ix_patient_contacts_primary_unique is a non-deferrable unique
        -- index and would reject a second TRUE within the statement.
        IF EXISTS (SELECT 1 FROM patient_contacts
                    WHERE id = ANY(loser_ids) AND is_primary) THEN
            DELETE FROM patient_contacts WHERE id = ANY(loser_ids);
            UPDATE patient_contacts SET is_primary = TRUE, updated_at = now()
             WHERE id = winner_id AND NOT is_primary;
        ELSE
            DELETE FROM patient_contacts WHERE id = ANY(loser_ids);
        END IF;

        RAISE NOTICE 'patient_contacts: merged % into % for patient % (%)',
            loser_ids, winner_id, g.patient_id, g.email;
    END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(_MERGE)
    op.execute(
        "CREATE UNIQUE INDEX uq_patient_contacts_patient_email "
        "ON patient_contacts (patient_id, email) WHERE email IS NOT NULL"
    )


def downgrade() -> None:
    # The merge is not reversible; only the constraint is dropped.
    op.execute("DROP INDEX IF EXISTS uq_patient_contacts_patient_email")
