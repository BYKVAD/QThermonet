# Contributing to QThermonet

This document describes the branching, merging, and versioning conventions used in this
repository. The same conventions apply across sibling repos that this plugin depends on
or that depend on it — keep them in sync if you're setting one up elsewhere.

## Branches

- **`main`** — the stable branch. Other repos and users depend on this branch being in a
  releasable state at all times. Protected: changes land only via pull request.
- **`dev`** — the integration branch. All new work is based on `dev`, not `main`.
- **`feature/*`, `fix/*`** — short-lived branches, forked from `dev`, merged back into
  `dev` via pull request.
- **`hotfix/*`** — for urgent fixes that can't wait for the normal `dev` → `main` cycle.
  Forked from `main`, merged into `main` via pull request, then merged back into `dev`
  afterward so the two branches don't drift apart.

## Opening a pull request

1. Branch from `dev` (`git checkout -b feature/your-feature dev`).
2. Make your changes, commit, and push.
3. Open a PR targeting `dev`. At least one approval is required before merging.
4. Prefer **squash merge** when merging a feature/fix branch into `dev` — keeps `dev`'s
   history to one commit per change.

Periodically, once `dev` is stable and ready to release, a PR is opened from `dev` into
`main`. Use a regular **merge commit** (not squash/rebase) for this — it preserves a
clear marker of what shipped in each release and makes reverting an entire release
straightforward if needed.

## Versioning

This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`):

- **MAJOR** — breaking changes.
- **MINOR** — new, backward-compatible functionality.
- **PATCH** — backward-compatible bug fixes.

Tags are cut on `main` only, at the point a `dev → main` PR merges, and are named
`vX.Y.Z` (matching the `version=` field in `metadata.txt`). Version bumps are currently
decided and tagged manually by the maintainer; this may become automated later, but the
tagging rule (tag `main` only, at release time) stays the same either way.

**If your repo depends on this plugin (or a pythermonet-based library), pin to a version
tag** (e.g. `@v0.2.0`), never to `@main` or `@dev` — those branch tips move underneath
you and aren't stable references.

## Branch protection

`main` and `dev` are both protected: direct pushes are rejected, and merging requires an
approved pull request. If your push is rejected, that's expected — open a PR instead.
Release tags (`v*`) are also protected from deletion or being re-pointed once pushed.
