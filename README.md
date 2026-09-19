# Contoso Motors — Real-Time Intelligence Demo

A complete, working Microsoft Fabric Real-Time Intelligence solution: factory machine
telemetry streams through **Eventstream** into **Eventhouse**, is visualized on a
**Real-Time Dashboard**, and drives two automated response paths — a sub-second
**Activator** rule and a governed, human-approved **Operations Agent** — both of which
run a Fabric notebook that emails maintenance and writes an auditable record.

Built and hardened against a live Fabric tenant: every command in the docs below has been
run and verified, including the failure modes and their fixes.

## Why this exists

Batch-based maintenance monitoring finds a developing fault hours after it starts. This
demo shows the alternative: detect a vibration anomaly on one machine, in a fleet of ten,
within seconds of it occurring — and turn that detection into an alert and an auditable
action automatically.

## What's in here

| Path | Purpose |
|---|---|
| [`deploy.py`](deploy.py) | Idempotent deployment of every Fabric artifact (Eventhouse, Eventstream, Activator, Operations Agent, Dashboard, notebooks) |
| [`simulator.py`](simulator.py) | Telemetry generator for 10 machines, with an injectable vibration/temperature fault |
| [`validate.py`](validate.py) | End-to-end validation of the deployed solution and live telemetry |
| [`preflight.py`](preflight.py) | Resumes a paused Fabric capacity and re-arms the Activator/Agent before a demo |
| [`kql/`](kql) | Table schema, ingestion mapping, and the analytics queries (peer-deviation anomaly detection, throughput, fault count) |
| [`power-automate/`](power-automate) | Reference template for an alternate notification path |
| [`tests/`](tests) | Unit tests for the simulator's event generation and connection handling |
| [`README-RTI.md`](README-RTI.md) | Full setup, deployment, and troubleshooting guide |
| [`DEMO-SCRIPT.md`](DEMO-SCRIPT.md) | A complete customer-facing demo script, business context, and a 3-minute live run |

## Quick start

```powershell
python -m pip install -r requirements.txt
az login
cp .env.example .env   # then fill in your own values - see README-RTI.md "Prerequisites"
python deploy.py
python validate.py
```

See [README-RTI.md](README-RTI.md) for prerequisites, required environment variables, and
step-by-step testing instructions, and [DEMO-SCRIPT.md](DEMO-SCRIPT.md) for the full demo
narrative and a 3-minute scripted run.

## Architecture

```
Machine telemetry (10 machines, every 2 seconds)
        │
        ▼
  Eventstream ──────────────► Eventhouse (KQL) ──► Real-Time Dashboard
        │                            │
        │                            └──────────► Operations Agent
        ▼                                              (governed, human-approved)
    Activator
   (seconds, automatic)
        │
        ▼
  Maintenance notebook ──► Email + auditable record
```

## License

[MIT](LICENSE)
