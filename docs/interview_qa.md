# Project interview questions

## Why build a Runtime instead of a single model call?

A model can propose useful actions but cannot reliably enforce budgets, permissions, persistence,
or completion criteria. The Runtime owns these deterministic controls and records their evidence.

## Why not use LangGraph?

The first version implements the loop directly so action validation, transaction boundaries,
recovery, retries, and termination semantics remain visible and explainable. A workflow framework
could be added later without changing the core contracts.

## Why is model `finish` not success?

It is only a request for verification. Tests, lint, protected-path checks, and other deterministic
verifiers decide whether the task reaches `COMPLETED`.

## What does Docker protect?

It provides a reproducible non-root, network-disabled, resource-limited command boundary. It is
not a complete production sandbox for malicious code; stronger deployments need separate nodes or
microVMs, hardened daemon access, image provenance, and platform security controls.

## How does recovery avoid replaying a patch?

Each mutating step stores an action fingerprint, workspace hash, and transaction state. On startup,
the Runtime compares the workspace with the pre-action hash before deciding whether an interrupted
step already changed the workspace.

## Why keep full history if the prompt is compressed?

Persistence serves audit and recovery; the prompt serves the next decision. Keeping them separate
allows bounded model inputs without destroying original evidence.

## What does the evaluation prove?

It compares four progressively controlled configurations on the same fixed tasks. The important
metrics distinguish claimed success from externally verified success and expose false completion,
recovery, policy enforcement, latency, and token cost.
