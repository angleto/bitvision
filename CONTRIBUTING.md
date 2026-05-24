# Contributing

Thanks for considering a contribution to **bitvision phoenix**.

The project is in alpha and under active development on the `v3.8`
line; the OSS release shipped 2026-05-19. Contributions are welcome
on both code and design: PRs targeting the issue tracker, design
discussion on the documents in `docs/`, and ADR proposals in
`docs/agents-api/decisions/` are all in-scope.

## Before opening a PR

1. Contributions are accepted under the project's
   [GNU AGPL-3.0-or-later](./LICENSE) license. Add a DCO sign-off to
   your commits (`git commit -s`, see
   https://developercertificate.org/) to certify you have the right
   to submit the work.
2. Read the relevant design doc:
   - Architecture → [`DESIGN.md`](./docs/DESIGN.md)
   - Permissions / sharing → [`docs/authorization.md`](./docs/authorization.md)
   - Data model → [`docs/data-model.md`](./docs/data-model.md)
3. Open a draft PR early — direction alignment matters more than a
   perfectly polished patch at this stage.

## Development environment

Prerequisites: Docker, Python 3.12, [uv](https://docs.astral.sh/uv/),
Node.js 22+, [pnpm](https://pnpm.io/).

```sh
cp .env.example .env
make up                     # starts postgres+pgvector, redis, minio, authentik
make backend.install
make workers.install
make frontend.install
make backend.dev            # API on http://localhost:8000 (OpenAPI at /docs)
make frontend.dev           # UI on http://localhost:3000
```

See the per-service `README.md` files in `backend/`, `workers/`,
`crawler/`, `mcp/`, `frontend/`, `infra/` for service-specific details.

## Code style

- Python: `ruff check` + `ruff format`. Type hints required on public
  functions. `mypy --strict` on new modules where practical.
- TypeScript: `biome check` (replaces ESLint + Prettier). Strict TS.
- Commit messages: Conventional Commits
  (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, …).

## Licensing

The project is licensed under **GNU AGPL-3.0-or-later** (see
[`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE)). Contributions are
accepted under the same license; a DCO sign-off (`git commit -s`) on
your commits certifies authorship and the right to submit.

## Mission

bitvision phoenix exists to **improve health** through open, trustworthy,
consent-based medical imaging infrastructure. Contributions that
undermine that mission — for example, features enabling surveillance,
social scoring, or non-consensual data extraction — will be declined.
