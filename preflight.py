"""Pre-demo readiness check.

Run this immediately before a live demo. It resumes the Fabric capacity and
re-arms the Activator rule, which Fabric silently stops whenever the capacity
is paused. A stopped rule looks perfectly healthy in the portal but will never
send an alert email, so this check exists to catch that before an audience does.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import requests

from deploy import (
    FabricClient,
    ensure_capacity_active,
    ensure_operations_agent,
    ensure_reflex,
    state_path,
)
from validate import decode_part, get_definition, token

ROOT = Path(__file__).resolve().parent


def rule_settings(workspace_id: str, activator_id: str) -> dict | None:
    definition = get_definition(workspace_id, activator_id)
    part = next(p for p in definition["parts"] if p["path"] == "ReflexEntities.json")
    entities = json.loads(base64.b64decode(part["payload"]).decode("utf-8"))
    for entity in entities:
        payload = entity.get("payload", {}).get("definition", {})
        if payload.get("type") == "Rule":
            return payload.get("settings")
    return None


def eventstream_topology(workspace_id: str, eventstream_id: str) -> dict:
    """Read the Eventstream topology, tolerating a mid-flight capacity pause.

    The autopause runbook can suspend the capacity at any moment. Fabric then
    returns 404 for every item call, which looks like a missing artifact, so
    resume the capacity and retry instead of failing.
    """
    url = (
        f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
        f"/eventstreams/{eventstream_id}/topology"
    )
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            return _get_topology(url)
        except (requests.HTTPError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as error:
            last_error = error
            if attempt == 4:
                break
            print("  Fabric call failed; re-checking capacity and retrying...")
            ensure_capacity_active()
            time.sleep(10)
    raise RuntimeError(
        "Could not read the Eventstream topology. The Fabric capacity may be "
        f"suspending faster than it can be used. Last error: {last_error}"
    )


def _get_topology(_url: str) -> dict:
    headers = {"Authorization": f"Bearer {token('https://api.fabric.microsoft.com')}"}
    response = requests.get(_url, headers=headers, timeout=90)
    if response.status_code == 404:
        raise requests.HTTPError("404 - capacity may have paused", response=response)
    response.raise_for_status()
    return response.json()


def node_states(topology: dict) -> dict[str, str]:
    return {
        node["name"]: node.get("status")
        for group in ("sources", "destinations", "streams")
        for node in topology.get(group, [])
    }


def ensure_eventstream_running(workspace_id: str, eventstream_id: str) -> bool:
    """Resume the Eventstream if any node is paused.

    Pausing the capacity pauses the Eventhouse destination, and resuming the
    capacity does not restart it. The source keeps accepting events, so the
    simulator still reports success while nothing reaches the KQL table.
    """
    states = node_states(eventstream_topology(workspace_id, eventstream_id))
    if all(state == "Running" for state in states.values()):
        print("PASS: Eventstream source, stream and destination are running.")
        return True

    stopped = {name: state for name, state in states.items() if state != "Running"}
    print(f"Eventstream nodes not running: {stopped}; resuming...")
    headers = {
        "Authorization": f"Bearer {token('https://api.fabric.microsoft.com')}",
        "Content-Type": "application/json",
    }
    resume = requests.post(
        f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
        f"/eventstreams/{eventstream_id}/resume",
        headers=headers,
        json={"startType": "Now"},
        timeout=120,
    )
    resume.raise_for_status()
    for _ in range(20):
        time.sleep(15)
        try:
            states = node_states(eventstream_topology(workspace_id, eventstream_id))
        except RuntimeError as error:
            print(f"  {error}")
            return False
        if all(state == "Running" for state in states.values()):
            print("PASS: Eventstream resumed; all nodes running.")
            return True
    print(f"FAIL: Eventstream nodes did not reach Running: {states}")
    return False


def main() -> int:
    ensure_capacity_active()
    state = json.loads(state_path().read_text(encoding="utf-8"))
    workspace_id = state["workspace"]["id"]
    activator_id = state["activator"]["id"]

    settings = rule_settings(workspace_id, activator_id)
    if settings is None:
        print("FAIL: ACT_MachineHealth has no Rule entity. Run deploy.py.")
        return 1

    if settings.get("shouldRun") is True:
        print("PASS: Activator rule 'High vibration' is running.")
    else:
        print("Activator rule is stopped; re-arming...")
        client = FabricClient()
        ensure_reflex(client, workspace_id, state["eventstream"]["id"])
        settings = rule_settings(workspace_id, activator_id)
        if not (settings or {}).get("shouldRun") is True:
            print(f"FAIL: could not re-arm the Activator rule. Settings: {settings}")
            return 1
        print("PASS: Activator rule re-armed and running.")

    agent = decode_part(
        get_definition(workspace_id, state["operationsAgent"]["id"]),
        "Configurations.json",
    )
    if agent.get("shouldRun") is True:
        print("PASS: Operations Agent 'OA_FactoryOperations' is running.")
    else:
        print("Operations Agent is stopped; re-arming...")
        ensure_operations_agent(
            FabricClient(),
            workspace_id,
            state["kqlDatabase"]["id"],
            state["maintenanceNotebook"]["id"],
        )
        agent = decode_part(
            get_definition(workspace_id, state["operationsAgent"]["id"]),
            "Configurations.json",
        )
        if agent.get("shouldRun") is not True:
            print("FAIL: could not re-arm the Operations Agent.")
            return 1
        print("PASS: Operations Agent re-armed and running.")

    if not ensure_eventstream_running(workspace_id, state["eventstream"]["id"]):
        return 1

    print("Ready. Run: python simulator.py --duration 120")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
