# Contoso Motors — Real-Time Intelligence Demo Script

**Duration:** 3 minutes live (12–15 minutes with narrative)
**Workspace:** `Contoso-Motors-Demo`
**Audience:** Manufacturing / operations / data leadership

---

## 1. The business context

**Contoso Motors** runs two plants — Chennai and Pune — across three production
lines, with ten CNC machines producing drivetrain components.

**The problem they came to us with:**

> "We find out a machine failed when the line stops. By then we've lost a shift."

Their existing analytics were **batch**. Telemetry landed in a warehouse overnight,
reports refreshed at 6am, and an engineer reviewed dashboards the next morning.
A bearing that started degrading at 2pm was discovered at 9am the following day —
roughly **19 hours** of blind operation.

**What that costs, per unplanned stoppage:**

| Impact | Value |
|---|---|
| Unplanned downtime | 4–8 hours |
| Lost production | ~£45,000 per hour on a drivetrain line |
| Emergency callout vs. planned maintenance | 3–5× the cost |
| Scrap from out-of-tolerance parts | Entire batch since drift began |

The ask was not "better dashboards." It was: **detect the condition while the
machine is still running, and act on it before it fails.**

---

## 2. What we built

A complete real-time pipeline in **Microsoft Fabric** — one platform, no glue code,
no separate Azure resources to manage.

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

### The Fabric components — and why each one

| Component | Item | What it does | Why it matters |
|---|---|---|---|
| **Eventstream** | `ES_MachineTelemetry` | Ingests telemetry via a custom endpoint, fans out to two destinations | No Event Hub, no Stream Analytics job, no infrastructure to size |
| **Eventhouse** | `EH_ContosoMotors` | KQL database purpose-built for time-series at scale | Sub-second queries over billions of events |
| **Real-Time Dashboard** | `RTD_FactoryFloor` | Live operational view, 10-second refresh | Queries Eventhouse *directly* — no semantic model refresh lag |
| **Activator** | `ACT_MachineHealth` | Streaming rule: acts on the event as it arrives | Detection to action in **seconds**, unattended |
| **Operations Agent** | `OA_FactoryOperations` | Agentic layer that reasons over telemetry and proposes action | Human-approved, auditable — governance where it matters |

**The architectural point to land:** this is one platform. Ingestion, storage,
analytics, visualisation, automation and AI in a single workspace, with one
security model and one bill.

---

## 3. The data and the anomaly

### Normal operation

Ten machines (`M-001`–`M-010`), one event each per **2 seconds** — about **300
events/minute**. Each event carries:

```json
{
  "machineId": "M-007", "line": "Line-C", "plant": "Plant-Pune",
  "timestamp": "2026-08-25T11:40:12Z",
  "temperatureC": 68.4, "vibrationMm": 3.7,
  "rpm": 1175, "throughputUnits": 44, "status": "running"
}
```

| Signal | Healthy range |
|---|---|
| Temperature | 60–75 °C |
| **Vibration** | **2.0–5.0 mm** |
| RPM | 1000–1300 |
| Throughput | 30–55 units |

### The injected fault — M-007

We deliberately degrade **one machine on Line-C at Plant-Pune**:

| Signal | Fault range |
|---|---|
| Temperature | 85–100 °C |
| **Vibration** | **8.1–14.0 mm** |
| Status | `fault` |

**Why vibration?** It is the leading indicator of bearing and spindle failure.
Vibration rises measurably *before* temperature spikes and long before the machine
stops. Catching it at 8 mm means a planned bearing swap; missing it means a seized
spindle and a scrapped batch.

**Why one machine and not all ten?** Because a controlled signal proves the
detection is *selective*. Nine machines stay healthy throughout — no false
positives. That is the claim that survives scrutiny.

**The 8.0 mm threshold** is the engineering damage limit for this machine class.
Above it, continued operation accelerates wear non-linearly.

---

## 4. Two speeds of response — the core message

This is the most important idea in the demo.

| | **Activator** | **Operations Agent** |
|---|---|---|
| Trigger | Per event, streaming | Periodic evaluation (~5 min) |
| Latency | **Seconds** | Minutes |
| Decision | Deterministic threshold | Reasons over context |
| Action | **Automatic, unattended** | **Requires human approval** |
| Use for | Safety-critical, unambiguous | Judgement, cost, prioritisation |

> "You don't want a human in the loop when vibration crosses a damage threshold —
> that's a deterministic safety rule, and Activator fires it in seconds. But you
> *do* want a human approving a £40,000 maintenance callout. That's the Operations
> Agent. Same data, two speeds, matched to the stakes."

That contrast is what separates this from a dashboard demo.

---

## 5. THE 3-MINUTE LIVE RUN

### Before the meeting (do this 5 minutes ahead)

```powershell
cd path\to\contoso-rti
python preflight.py
```

Must show all PASS lines. This resumes the capacity and re-arms the Activator —
**the capacity auto-pauses after 30 minutes idle**, and that also pauses the
Activator. Never skip it.

Open these tabs in advance:

1. `RTD_FactoryFloor`
2. `ACT_MachineHealth` → **History** (set range to **Last hour**)
3. Your inbox
4. PowerShell in the project folder

---

### T+0:00 — Start the stream

```powershell
python simulator.py --duration 480 --anomaly-machine M-007 --anomaly-start 120 --anomaly-window 180
```

> "Ten machines across two plants are now streaming live into Fabric — temperature,
> vibration, RPM, throughput, every two seconds. This is going through an Eventstream
> custom endpoint. No Event Hub, no infrastructure."

**Show:** `RTD_FactoryFloor` — throughput by line, all machines in the 2–5 mm band,
fault count **0**.

---

### T+0:40 — Show it's genuinely live

Point at the vibration tile. Every machine sits in a tight healthy band.

> "This dashboard queries the Eventhouse directly. It's not waiting for a scheduled
> refresh — this is the factory floor, right now, ten seconds behind reality."

Optionally flip the **Plant** or **Line** filter to show slicing.

---

### T+2:00 — The failure appears

At the two-minute mark M-007 begins degrading. **Say nothing — let them notice.**

> "There it is. M-007, Line-C, Plant-Pune. Vibration has jumped from about 3.5
> millimetres to over 11. Temperature is climbing past 90. The other nine machines
> are completely unaffected."

**Show on the dashboard:**
- M-007 separating sharply from the pack
- **Fault machines → 1**
- Active anomalies table populating

---

### T+2:30 — The response

Switch to `ACT_MachineHealth` → **History**.

> "The Activator saw the vibration cross 8 millimetres and fired — automatically,
> within seconds of the event arriving. No polling, no batch job, no human."

Switch to your inbox. **The email is already there:**

```
Subject: Predictive maintenance required - M-007 [ACT_MachineHealth (Activator)]

Machine: M-007
Line: Line-C
Plant: Plant-Pune
Vibration: 13.8 mm
Event time (UTC): 2026-08-25T11:40:12Z

Vibration 13.8 mm exceeded the 8.0 mm maintenance threshold.

Detected by: ACT_MachineHealth (Activator)
Streaming rule ACT_MachineHealth evaluated this event within seconds of arrival.
```

> "Maintenance has the machine ID, the line, the plant, the reading and the exact
> timestamp. They can act on this immediately — while the machine is still running."

---

### T+3:00 — The audit trail

In the KQL queryset:

```kusto
MaintenanceRequests
| order by requestedAt desc
| take 5
```

> "And every automated action is logged. Who raised it, which machine, what reading,
> what time. Full auditability — this is a governed process, not a script sending
> emails."

**Stop here. Three minutes, complete story.**

---

## 6. If you have more time

### The anomaly detection (2 min)

Open `KQLQ_FactoryAnalytics` → **Active anomalies**:

```kusto
let win = 20m; let step = 10s; let minVib = 8.0; let minScore = 3.0;
let binned = MachineTelemetry
  | where timestamp > ago(win)
  | summarize vib = avg(vibrationMm), maxTemp = max(temperatureC),
              faults = countif(status == 'fault')
      by machineId, plant, line, ts = bin(timestamp, step);
let fleet = binned
  | summarize fleetMedian = percentile(vib, 50), fleetStd = stdev(vib) by ts;
binned
| join kind=inner fleet on ts
| extend score = iff(fleetStd > 0.0, (vib - fleetMedian) / fleetStd, 0.0)
| where vib > minVib and score > minScore
| order by timestamp desc
```

> "This isn't just a threshold. It compares each machine against its peers in the
> same ten-second window. A machine is flagged only when it breaches the damage
> limit *and* deviates sharply from the rest of the fleet. That's how you avoid
> alerting on a plant-wide condition like ambient temperature — and it's native KQL,
> no ML model to train, deploy or maintain."

### The Operations Agent (2 min — describe, don't run)

Open `OA_FactoryOperations` and show its configuration.

> "The Activator is deterministic — a threshold, in seconds, unattended. The
> Operations Agent is the layer above: it understands the semantics of this data —
> machines, lines, plants, what vibration means — and reasons about the condition
> rather than just matching a number.
>
> Critically, it doesn't act on its own. It proposes an action and requests
> approval before anything happens. That's deliberate: the agent handles judgement,
> and a human stays accountable for the decision."

**Note for you, not the customer:** the agent's approval flow routes through Teams,
which isn't licensed in this demo tenant, so don't attempt to run it live. Show the
configuration and the design intent. It is a legitimate and compelling part of the
story told this way.

---

## 7. Business impact — the close

| Before | After |
|---|---|
| Fault found next morning | Detected in **seconds** |
| ~19 hours blind | Real-time visibility |
| Reactive emergency repair | Planned intervention |
| Batch reports at 6am | Live dashboard, 10s refresh |
| Manual triage | Automated alert + audit trail |

**Per avoided stoppage:**

- 4–8 hours of downtime avoided → **£180,000–£360,000** in retained production
- Planned vs. emergency maintenance → **3–5× lower** repair cost
- Scrap avoided from the moment drift begins
- Engineers work from live conditions instead of yesterday's report

> "One avoided stoppage pays for the platform. And this generalises — the same
> pattern applies to any high-frequency operational signal: energy, logistics,
> payments, network telemetry. What changes is the data, not the architecture."

### Closing statement

> "In three minutes you saw telemetry stream in, a fault emerge, an anomaly detected
> against fleet behaviour, an automated alert fire in seconds, and an auditable
> record created — all inside one Fabric workspace.
>
> No Event Hub. No Stream Analytics. No Databricks cluster. No ML model to operate.
> One platform, one security model, one bill.
>
> Contoso Motors moved from finding out the next morning, to knowing within seconds
> — and that's the difference between a repair and a shutdown."

---

## 8. Anticipated questions

**"Does this scale beyond ten machines?"**
Eventhouse handles millions of events per second and is used in production for
Microsoft-scale telemetry. Ten machines at 300 events/minute is a demo, not a limit.

**"What if we already have historical data?"**
Eventhouse holds both. Real-time and historical query in the same KQL, same engine.
You can also mirror or shortcut existing lakehouse data without moving it.

**"How is this different from Stream Analytics / Databricks?"**
Those are components you assemble and operate. This is one platform with ingestion,
storage, analytics, visualisation, automation and AI already integrated — governed
by Purview, secured once.

**"Why not just an ML model?"**
For this signal you don't need one — peer-deviation detection in native KQL is
transparent, explainable, and has nothing to retrain or drift. Fabric supports ML
when the problem genuinely needs it; the discipline is not reaching for it first.

**"What about false positives?"**
Watch the demo — nine machines stayed healthy throughout. Detection requires both a
damage-threshold breach *and* significant deviation from fleet behaviour. Both
conditions, or no alert.

**"Can the alert do more than email?"**
Yes — here it runs a Fabric notebook, which could equally open a work order in SAP
or Dynamics, trigger a pipeline, or post to a queue. Email is the simplest thing to
show in three minutes.

---

## 9. Pre-flight checklist

- [ ] `python preflight.py` — all PASS (run **immediately** before, not an hour ahead)
- [ ] `RTD_FactoryFloor` open and rendering
- [ ] `ACT_MachineHealth` → History, range set to **Last hour**
- [ ] Inbox open; check ACS mail isn't in Junk
- [ ] PowerShell in the project folder, command pre-typed
- [ ] Previous test data aged out (dashboard tiles use 2–5 min windows)

**If something looks wrong mid-demo:** the capacity has almost certainly auto-paused.
`python preflight.py` fixes it in about 60 seconds. Keep the simulator running
between segments so the capacity stays warm.
