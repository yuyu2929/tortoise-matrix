# Contributing

Thanks for considering a contribution to Tortoise.

## Ground rules

1. **The invariant is sacred**: the UI only ever reads from the local
   store; no code path may make a user-facing operation await the network.
   If your change needs to "just wait a moment for the server", redesign
   it as an optimistic event plus a queued job.
2. **Every guarantee needs a test**, ideally in `tests/test_slow_server.py`
   where the server is hostile by default.
3. **Failures are events, not exceptions**: new failure modes should emit
   a `TortoiseEvent` the UI can render, not raise into the caller.

## Setup

```bash
git clone <your fork>
cd tortoise-matrix
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest tests/ -q
```

## Workflow

1. Pick or open an issue. Ideas live in
   [open-source-ideas/ideas](https://github.com/open-source-ideas/ideas/issues/430).
2. Branch from `main`, keep commits focused.
3. `pytest tests/ -q` must be green.
4. Update `README.md` / `ARCHITECTURE.md` if you changed a documented
   behavior.
5. Open a pull request describing *what breaks without your change*.

## Good first contributions

- Harden `RealMatrixTransport` against a live Synapse (login flows,
  reconnection, backoff on 429).
- A CLI client (`examples/`) exercising the event stream.
- Metrics export (Prometheus) from `QueueMetrics`.
- i18n for the demo page.

## License

By contributing you agree your work is dual-licensed under the project's
LICENSE (AGPL-3.0 community tier, with the option to include it in the
commercial tier).
