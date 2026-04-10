# Ferrite — Project Instructions

## Project Overview

Ferrite is an autonomous storage diagnostics and data recovery tool written in pure Rust.
See `docs/architecture.md` for the full crate dependency diagram and design rationale.

## Crate Naming

All crates use the `ferrite-` prefix: `ferrite-core`, `ferrite-blockdev`, etc.
The binary is named `ferrite`.

## Architecture Rules

1. **Pure Rust only** — no FFI with C libraries (libtsk, libparted, libsmartmon rejected).
2. **Three-layer architecture:** Reasoning (logic) / Memory (state) / Tools (side effects).
3. **Traits over concretions** — `BlockDevice`, `FilesystemParser`, etc. are traits; platform impls live in separate modules.
4. **`thiserror` in library crates, `anyhow` in binary** — never swap these.
5. **`tracing` for all logging** — subscriber configured only in `src/main.rs`.
6. **Read-only access to source drives** — never write to the source device.

## Phase Dependency Order

```
Phase 0 → Phase 1 → Phase 2
                  → Phase 3 (parallel)
                  → Phase 4 → Phase 5
                  → Phase 6
                            → Phase 7 (TUI, integrates all)
```

## Test Requirements

Every phase must pass before moving to the next:
- `cargo test --workspace`
- `cargo clippy --workspace -- -D warnings`
- `cargo fmt --check`

## Change Log

Every phase must produce `aiChangeLog/phase-XX.md` before the phase commit.
