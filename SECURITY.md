# Security Policy

bitvision phoenix is an **early-stage, pre-1.0 open-source project**.
It is **not a certified medical device** (not CE/MDR, IVDR, or FDA
cleared) and is intended for personal health-record use, research, and
education only. Security is best-effort and provided **without
warranty** (see [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE)).

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Email **security@bitvision.example** (or the maintainer contact listed
in [`CONTRIBUTING.md`](./CONTRIBUTING.md)) with:

- a description of the issue and its impact,
- steps to reproduce (proof-of-concept if available),
- affected component / version / commit.

We aim to acknowledge a report within **72 hours** and to agree a
remediation timeline with you. Please allow **coordinated disclosure**:
do not publish details until a fix has shipped in a release, or 90 days
have elapsed, whichever comes first. Reporters are credited in the
[`CHANGELOG.md`](./CHANGELOG.md) unless they ask otherwise.

> Replace `security@bitvision.example` with the real disclosure mailbox
> for your deployment/fork before publishing.

## Scope

In scope: the application code in this repository (backend, workers,
crawler, MCP server, frontend) and its default configuration.

Out of scope: third-party dependencies (report upstream), any specific
production deployment or its infrastructure (deployment manifests and
secrets are intentionally **not** part of this repository), and issues
that require a compromised operator account or host.

## Known limitations (pre-1.0)

- Not for production clinical use without an independent risk
  assessment by the operator.
- AI-generated content is **not reviewed by a clinician** before
  display; it is always labelled `author_kind=agent` and badged in the
  UI, but the operator and end user remain responsible for any use.
- GDPR / national health-data compliance (lawful basis, consent,
  retention, breach notification, data-subject rights) is the
  **operator's responsibility**, not a guarantee of the software.
- Public, unauthenticated surfaces (e.g. share links, calendar
  subscription feeds) expose the data they are scoped to anyone who
  holds the URL until revoked — treat those URLs as secrets.

## Hardening references

Implementation notes live under [`docs/`](./docs/), e.g.
`docs/security-jwt-and-secrets.md`,
`docs/security-encryption-at-rest-envelope.md`, and the
authorization / sharing design docs linked from the
[`README`](./README.md). Operators **must** set strong secrets
(`BVP_JWT_SECRET`, `BVP_BYOK_MASTER_KEY`, …) before any non-local
deployment; the backend refuses to boot in `production` with a weak or
default JWT secret.
