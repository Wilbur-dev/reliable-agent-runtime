# Architecture

The runtime separates model proposals from trusted control decisions:

```text
API / CLI
    |
Task state + budgets
    |
Context Builder -> LLM -> structured action
                         |
                  Policy Engine
                  /     |       \
              allow    deny    approval
                |                |
          Tool Registry <--------+
                |
       workspace / Docker command
                |
     SQLite events + Verifiers + Prometheus
                |
       continue / complete / terminate
```

The model can propose an action and request completion. It cannot directly change task state,
bypass tool schemas, approve a risky action, or mark external verification successful.

## Trust boundaries

- The API and Runtime own lifecycle state and persistence.
- The Policy Engine evaluates actions before side effects.
- File tools resolve paths against one workspace; protected paths remain immutable.
- Configured test and lint commands run in a restricted Docker container.
- Verifiers, rather than model prose, decide whether a write task is complete.
- Full trajectories stay in SQLite while Context Builder creates a bounded model view.

Docker limits accidental host impact but is not presented as a production malicious-code sandbox.
