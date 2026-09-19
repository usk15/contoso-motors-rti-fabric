# Contoso Motors Real-Time Intelligence demo

## Prerequisites

- Python 3.11+, Azure CLI (`az login` with access to your Fabric capacity and workspace).
- A Microsoft Fabric capacity you can administer (Trial or paid F-SKU).
- An Azure Communication Services resource with a verified email domain, for the
  maintenance notebook's email action (Fabric-native email requires a licensed Exchange
  mailbox, which many demo/dev tenants do not have).

Set these environment variables before running `deploy.py` (see `.env.example`):

| Variable | Required | Purpose |
|---|---|---|
| `FABRIC_CAPACITY_ID` | Yes | Fabric capacity GUID the workspace is assigned to. |
| `FABRIC_CAPACITY_RESOURCE_ID` | Yes | Full Azure resource ID of that capacity, used to resume it if paused. |
| `ACS_NAME` / `ACS_RESOURCE_GROUP` | Yes (or `ACS_CONNECTION_STRING`) | Your Azure Communication Services resource. |
| `ACS_SENDER_ADDRESS` | Yes | A verified sender address on that resource's email domain. |
| `MAINTENANCE_EMAIL_TO` | No (default `maintenance-team@example.com`) | Recipient for maintenance alerts. |
| `OPERATIONS_AGENT_RECIPIENT` | No (defaults to the signed-in `az` user) | Operations Agent's in-tenant notification UPN. |
| `RTI_STATE_FILE` | No (default `deployment-state.json`) | Lets one checkout deploy to more than one tenant without overwriting state. |

## Deployment

Run from PowerShell:

```powershell
python -m pip install -r requirements.txt
az login
python deploy.py
python validate.py
```

`python validate.py` validates deployment configuration and reports recent anomaly results without requiring one. After running the anomaly simulator, use `python validate.py --require-anomaly` to enforce the end-to-end anomaly acceptance check.

The deployment is idempotent. Set `FABRIC_CAPACITY_ID` (the Fabric capacity GUID, not its
Azure resource ID) and `FABRIC_CAPACITY_RESOURCE_ID` (the full Azure resource ID, used to
resume a paused capacity) before running `deploy.py` - see **Prerequisites** below.

## Deployed Fabric artifacts

`deploy.py` creates these items in your target workspace. IDs are assigned by Fabric at
deploy time and recorded in `deployment-state.json` (not checked into source control,
since it is deployment-specific output, not code).

| Artifact | Type |
|---|---|
| `EH_ContosoMotors` | Eventhouse |
| `ContosoTelemetry` | KQL Database |
| `ES_MachineTelemetry` | Eventstream |
| `NB_TelemetrySimulator` | Notebook |
| `NB_MaintenanceAction` | Notebook |
| `KQLQ_FactoryAnalytics` | KQL Queryset |
| `ACT_MachineHealth` | Activator (Reflex) |
| `OA_FactoryOperations` | Operations Agent |
| `RTD_FactoryFloor` | Real-Time Dashboard |

After deploying, open your workspace at `https://app.fabric.microsoft.com/groups/<your-workspace-id>`
(the ID is printed by `deploy.py` and saved in `deployment-state.json`).

## Testing real-time streaming and the Activator

### Step 0 - Preflight (always run this first)

```powershell
cd contoso-rti
python preflight.py
```

This resumes the Fabric capacity, re-arms the Activator rule and the Operations Agent, and
resumes the Eventstream. **Pausing the capacity silently stops all three.** They still look
correct in the portal, and the Eventstream source keeps accepting events while its
destination is paused - so the simulator reports success while nothing reaches the KQL
table. Never skip this step.

### Step 1 - Credentials (automatic)

The simulator reads the current Custom Endpoint connection string from the Fabric REST API
(`/eventstreams/{id}/sources/{id}/connection`), so no manual copying is needed and stale
credentials are no longer possible. Just be signed in with `az login`.

To override - for example to target a different endpoint - set `EVENTSTREAM_CONN_STR`
(single-quote it in PowerShell; the `;` characters break double-quoted values).

### Step 2 - Test real-time streaming (normal traffic)

```powershell
python simulator.py --duration 120
Start-Sleep -Seconds 30
python validate.py --require-fresh
```

Expect `Sent ~600 telemetry events.` and `PASS: ... rows`. `--require-fresh` is the real
test: it fails unless both the event time and the ingestion time are within five minutes,
so stale rows cannot produce a false pass. Open `RTD_FactoryFloor` while this runs; tiles
refresh every 10 seconds. All ten machines should sit in the 2-5 mm vibration band with
zero faults.

If `--require-fresh` fails, run `python preflight.py` - the Eventstream destination is the
usual cause.

### Step 3 - Test the Activator (anomaly traffic)

```powershell
python simulator.py --duration 420 --anomaly-machine M-007 --anomaly-start 150 --anomaly-window 240
Start-Sleep -Seconds 30
python validate.py --require-fresh --require-anomaly
```

`--anomaly-start` matters for the Activator. The rule triggers on a **transition**
(`BecomesGreaterThan 8.0`) over a 1-minute tumbling average, so M-007 must report healthy
values first. If the anomaly starts at t=0 there is no transition to detect and the rule
never fires. The default is a 90-second baseline; use 150 seconds to give the tumbling
window at least two clean healthy buckets before the spike.

`M-007` runs at 8.1-14.0 mm and 85-100 C with status `fault` for the first six minutes,
then returns to normal. Expected observations:

| Where | What to expect |
| --- | --- |
| Dashboard | `M-007` separates from the fleet band; "Fault machines" shows `1`; the anomaly tile populates after ~2 minutes |
| `validate.py` | `PASS: M-007 anomaly rows detected...` |
| Email | Alert to the `MAINTENANCE_EMAIL_TO` address, typically 2-5 minutes after the breach |

The rule averages vibration over a 1-minute tumbling window and fires on
`BecomesGreaterThan 8.0`, so it needs roughly a full minute above the threshold before it
triggers. Keep the anomaly window at 300 seconds or more.

To confirm the rule fired, open `ACT_MachineHealth` in the portal and check the
**High vibration** rule's activation history.

### Step 4 - Test the Operations Agent

The Operations Agent evaluates on its own cycle (roughly every five minutes), so it needs a
longer breach than the Activator:

```powershell
python simulator.py --duration 600 --anomaly-machine M-007 --anomaly-window 540
```

While that runs, open `OA_FactoryOperations` in the portal and watch its activity. When it
sees M-007 above 8.0 mm it recommends the `SendMaintenanceEmail` action. Approve the
recommendation, and it runs `NB_MaintenanceAction`, which emails the configured
`MAINTENANCE_EMAIL_TO` address and writes an auditable row:

```kusto
MaintenanceRequests
| order by requestedAt desc
| take 10
```

#### How the email works

Tenants without Exchange mailboxes can't rely on Fabric-native email, so the notebook
sends through **Azure Communication Services** (your `ACS_NAME` / `ACS_RESOURCE_GROUP`)
using a verified Azure-managed sender domain. Verified working end to end.

The ACS access key is fetched from Azure at deploy time and injected into the deployed
Fabric notebook, so it never appears in this repository. Anyone with workspace access can
read it in `NB_MaintenanceAction`, which is acceptable for this test environment only -
rotate the key before reusing this pattern anywhere else.

Overrides: `ACS_NAME`, `ACS_RESOURCE_GROUP`, `ACS_CONNECTION_STRING`, `ACS_SENDER_ADDRESS`,
`MAINTENANCE_EMAIL_TO`.

The agent's own *notification* (separate from the action email) must use an in-tenant UPN -
Fabric rejects external addresses and guest UPNs alike, even after granting the guest
workspace access. Override with `OPERATIONS_AGENT_RECIPIENT`.

### One-command alternative

```powershell
.\run-demo.ps1 -Mode anomaly   # or: -Mode normal
```

This runs preflight, prompts for the credentials without echoing them, runs the simulator,
validates, and clears the environment variables afterwards.

### Why a Fabric capacity may keep pausing

Some organizations run a cost-control automation that suspends idle Fabric capacities -
for example, an Azure Automation runbook on a schedule that checks the Fabric Admin
Activity Events API and suspends the capacity after a period with zero activity. If your
capacity has one, it has two demo consequences:

1. A pause stops the Activator rule, the Operations Agent, and the Eventstream destination.
   Resuming the capacity does **not** restart them, which is why `preflight.py` exists.
2. Fabric returns `404 Not Found` on every item call while paused, which looks like a
   missing artifact rather than a paused capacity.

Practical guidance: keep working during a demo. Querying, dashboard views and notebook runs
all register as activity, so a live demo keeps itself alive. A quiet gap of 30+ minutes -
for example between rehearsal and the real session - can trigger a pause, so re-run
`python preflight.py` before you start.

If you manage such an automation yourself, disable its schedules for the demo window and
re-enable them afterwards, for example:

```powershell
$rg = "<your-resource-group>"; $aa = "<your-automation-account>"
az automation schedule list --automation-account-name $aa --resource-group $rg --query "[].name" -o tsv |
    ForEach-Object { az automation schedule update --automation-account-name $aa --resource-group $rg --name $_ --is-enabled false }
```

Re-enable with `--is-enabled true` when finished, or the capacity will bill continuously.

### Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Sent N events` but no rows in KQL | **Eventstream destination paused** (capacity pause does this; the source stays Running so sending still succeeds) | `python preflight.py` |
| `404 Not Found` on Fabric calls | Capacity paused | `python preflight.py` |
| Data lands but no email | Activator rule stopped, **or the Eventstream has no Activator destination** | `python preflight.py`, then `python validate.py` |
| Activator shows zero activations | Anomaly started at t=0, so there was no healthy-to-fault transition to detect | Use `--anomaly-start 150` |
| Activator shows zero activations despite a clean transition | Attribute wired to the source event instead of a `SplitEvent` view | Re-run `deploy.py` (now builds the full chain) |
| Rule activates but no email | Action is Fabric-native email; some tenants have no mailboxes | Set the action to **Run Notebook** -> `NB_MaintenanceAction` in the portal |
| `Unable to convert vibrationMm to float` | Notebook parameter typed `Number`; Activator sends text | Re-run `deploy.py`, then **Save and update** the rule |
| Rule edited but behaviour unchanged | `shouldApplyRuleOnUpdate` was false, so the update never restarted the rule | Re-run `deploy.py` (now sets it true) |
| Agent never recommends anything | Agent stopped, or breach too short | `python preflight.py`; use a 540s anomaly window |
| `No module named 'azure.eventhub'` | Shared Python env lost the package | `python -m pip install -r requirements.txt` |
| Dashboard tiles empty | Tiles use short windows (2-5 min) | Keep the simulator running while presenting |
| `CBS Token authentication failed` | A manually set `EVENTSTREAM_CONN_STR` is stale | `Remove-Item Env:\EVENTSTREAM_CONN_STR` and let the simulator fetch it |

### Checking Eventstream health directly

```powershell
python -c "import json,preflight;s=json.load(open('deployment-state.json'));print(preflight.node_states(preflight.eventstream_topology(s['workspace']['id'],s['eventstream']['id'])))"
```

All three nodes - source, stream and destination - must report `Running`.

## Run the telemetry simulator

The simulator reads the current Custom Endpoint credentials from the Fabric REST API, so
no manual copying is required - just `az login` first.

```powershell
python simulator.py --duration 120
Start-Sleep -Seconds 30
python validate.py --require-fresh
python simulator.py --duration 420 --anomaly-machine M-007 --anomaly-window 360
```

Because credentials are read live, they are never stale even after the Eventstream topology
is updated. The simulator sends one event for each of `M-001` through `M-010` every two
seconds and handles Ctrl+C gracefully.

## Activator

`ACT_MachineHealth` is deployed with a complete, active rule set (no portal authoring required):

| Entity | Value |
|---|---|
| Source | `ES_MachineTelemetry` (streaming, not KQL polling) |
| Object | `Machine`, identified by `machineId` |
| Attribute | `vibrationMm` |
| Condition | 1-minute average vibration becomes greater than `8.0` |
| Action | Email to the `MAINTENANCE_EMAIL_TO` address |
| State | `shouldRun: true` |

The rule is built from the documented `ReflexEntities.json` contract, so redeploying restores it exactly.

### Activator action: use Run Notebook, not email

Some tenants have no Exchange mailboxes (for example, an `EXCHANGE_S_FOUNDATION`-only
license), so Fabric-native email never delivers there. If yours is one of them, set the
rule's action to **Run Notebook -> `NB_MaintenanceAction`**, which sends via Azure
Communication Services instead.

This must be set in the **portal**: some tenants' Activator REST API rejects
`fabricItemAction-v1` definitions (`Invalid definition.`), but the portal wires it correctly
regardless.

Open the rule -> Action -> **Run Notebook** -> `NB_MaintenanceAction` -> **Save and update**.
Use *Save and update*, not *Save* - only "update" applies the change to the running rule.

All notebook parameters are typed as **strings**. Activator passes parameters as text and
sends none at all on a test run, so a `Number`-typed parameter fails before any code runs
with `Unable to convert vibrationMm to float.` The notebook coerces values internally.

### The entity chain matters

Attributes must **not** read from the raw source event. Fabric requires a `SplitEvent` view
that partitions the stream by object identity, and attributes read from *that*:

```
eventstreamSource -> SourceEvent -> SplitEvent (keyed by machineId) -> vibrationMm -> Rule
                                         ^
                     machineId (IdentityPartAttribute) + machineId tuple (IdentityTupleAttribute)
```

Wiring `vibrationMm` directly to the source event is accepted by the API but the rule then
never evaluates and reports **zero activations** with no error anywhere. `IdentityTupleAttribute`
is also required - the identity part alone does not form a usable object key.

### The Activator needs an Eventstream destination

A correct rule is not enough. `ES_MachineTelemetry` must also have an **Activator destination**
pointing at `ACT_MachineHealth`; without it the rule receives no events and silently never
fires, while everything still looks healthy in the portal.

When Fabric creates that destination it injects **its own** `eventstreamSource-v1` entity into
the Activator, and only that entity carries live events. `deploy.py` therefore:

1. Creates the eventstream, then the Activator.
2. Adds the Activator destination to the eventstream.
3. Discovers Fabric's injected source (`find_live_reflex_source`) and rebinds the rule's event
   to it, keeping that entity in the definition so the reference still resolves.

`validate.py` asserts both the Activator destination and that the event's source reference
resolves to the live source, so this cannot regress unnoticed.

Tenant-specific constraints observed while implementing this (yours may differ):

- Some tenants reject `kqlSource-v1`; `eventstreamSource-v1` is used instead, which also
  gives true streaming input rather than polling.
- `IdentityPartAttribute` must contain only the `IdPartStep`/`TypeAssertion` step.
- The Activator definition must be sent without a `format` field.

Optional portal step: add the `MachineMaintenanceRequested` business event, which has no public API contract.

## Operations Agent

`OA_FactoryOperations` is deployed with:

- `ContosoTelemetry` as its KQL source
- An authored high-vibration playbook and ontology
- A `SendMaintenanceEmail` action definition with the five telemetry parameters
- A threshold rule for vibration above `8.0`
- `shouldRun: true`

It evaluates on a periodic cycle (a few minutes) rather than streaming per event, and its
recommendation requires human approval before anything runs - by design, this is the
governed/agentic path, distinct from the Activator's unattended one.

To complete the action so an approval actually sends mail:

1. Open `OA_FactoryOperations` in the portal.
2. Open the `SendMaintenanceEmail` action.
3. Set it to **Run Notebook -> `NB_MaintenanceAction`** (the same notebook the Activator
   uses). The portal's parameter-mapping cache can go stale after the notebook's parameters
   change - if a run fails on a type-conversion error, re-pick the notebook in the action to
   refresh it.
4. Save, ensure the agent is running (`preflight.py` re-arms it if not), and start it.
5. Inject a sustained anomaly spanning at least one agent evaluation cycle (see Step 4
   above) and approve the recommendation when it appears.

`NB_MaintenanceAction` resolves the breaching machine from telemetry, emails
`MAINTENANCE_EMAIL_TO` via Azure Communication Services, and logs an audit row to
`MaintenanceRequests` - the same action path the Activator uses, so both routes are
provable end to end without any Power Automate/Outlook connector.

## Validation performed

- KQL table has the exact nine-column schema.
- `MachineTelemetryMapping` exists.
- Eventstream topology is Custom Endpoint to processed Eventhouse ingestion.
- Dashboard contains four tiles, Plant/Line filters, and 10-second refresh.
- Operations Agent definition contains its playbook and email action.
- 1,210 validation events were seeded directly into KQL.
- Live trend query returned data.
- The peer-deviation anomaly detector flagged `M-007` (11 consecutive 10-second buckets) with no false positives across the other nine machines.
- Fault-machine query returned one.
- Simulator unit tests pass.

Direct KQL seeding validates analytics but doesn't replace the Eventstream acceptance test. Run the simulator after securely copying the portal-only connection string.
