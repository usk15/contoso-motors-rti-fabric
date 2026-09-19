from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from deploy import (
    ALERT_RECIPIENT,
    ANOMALY_QUERY_BODY,
    ANOMALY_QUERY_HEADER,
    ensure_capacity_active,
    state_path,
)


ROOT = Path(__file__).resolve().parent

TRANSIENT = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


def with_retry(call, attempts: int = 4):
    """Fabric intermittently drops connections; retry rather than fail a demo."""
    for attempt in range(attempts):
        try:
            return call()
        except TRANSIENT:
            if attempt == attempts - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def token(resource: str) -> str:
    return subprocess.check_output(
        [
            "az", "account", "get-access-token", "--resource", resource,
            "--query", "accessToken", "--output", "tsv",
        ],
        text=True,
        shell=True,
    ).strip()


def get_definition(workspace_id: str, item_id: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token('https://api.fabric.microsoft.com')}"}
    url = (
        f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/"
        f"items/{item_id}/getDefinition"
    )
    response = with_retry(lambda: requests.post(url, headers=headers, timeout=90))
    response.raise_for_status()
    if response.status_code == 202:
        location = response.headers["Location"]
        while True:
            poll = with_retry(lambda: requests.get(location, headers=headers, timeout=90))
            poll.raise_for_status()
            if poll.json().get("status") == "Succeeded":
                response = with_retry(
                    lambda: requests.get(
                        location.rstrip("/") + "/result", headers=headers, timeout=90
                    )
                )
                response.raise_for_status()
                break
    return response.json()["definition"]


def decode_part(definition: dict[str, Any], path: str) -> dict[str, Any]:
    part = next(item for item in definition["parts"] if item["path"] == path)
    return json.loads(base64.b64decode(part["payload"]).decode("utf-8"))


def decode_text_part(definition: dict[str, Any], path: str) -> str:
    part = next(item for item in definition["parts"] if item["path"] == path)
    return base64.b64decode(part["payload"]).decode("utf-8")


def query(state: dict[str, Any], csl: str, management: bool = False) -> list[dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {token('https://api.kusto.windows.net')}",
        "Content-Type": "application/json",
    }
    endpoint = "/v1/rest/mgmt" if management else "/v1/rest/query"
    response = with_retry(
        lambda: requests.post(
            state["clusterUri"] + endpoint,
            headers=headers,
            json={"db": state["kqlDatabase"]["id"], "csl": csl},
            timeout=120,
        )
    )
    response.raise_for_status()
    table = response.json()["Tables"][0]
    columns = [column["ColumnName"] for column in table["Columns"]]
    return [dict(zip(columns, row)) for row in table["Rows"]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the deployed Contoso Motors RTI demo.")
    parser.add_argument(
        "--require-anomaly",
        action="store_true",
        help="Fail unless an M-007 anomaly is detected in the last 20 minutes.",
    )
    parser.add_argument(
        "--require-fresh",
        action="store_true",
        help="Fail unless telemetry was ingested with a current event timestamp in the last five minutes.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_capacity_active()
    state = json.loads(state_path().read_text(encoding="utf-8"))
    workspace_id = state["workspace"]["id"]

    schema = query(state, ".show table MachineTelemetry schema as json", management=True)
    ordered_columns = json.loads(schema[0]["Schema"])["OrderedColumns"]
    assert [column["Name"] for column in ordered_columns] == [
        "machineId", "line", "plant", "timestamp", "temperatureC",
        "vibrationMm", "rpm", "throughputUnits", "status",
    ]

    mappings = query(state, ".show table MachineTelemetry ingestion mappings", management=True)
    assert any(mapping["Name"] == "MachineTelemetryMapping" for mapping in mappings)

    eventstream = decode_part(
        get_definition(workspace_id, state["eventstream"]["id"]),
        "eventstream.json",
    )
    assert eventstream["sources"][0]["type"] == "CustomEndpoint"
    destination = next(
        d["properties"] for d in eventstream["destinations"] if d["type"] == "Eventhouse"
    )
    assert destination["dataIngestionMode"] == "ProcessedIngestion"
    assert destination["itemId"] == state["kqlDatabase"]["id"]
    assert destination["databaseName"] == "ContosoTelemetry"
    assert destination["tableName"] == "MachineTelemetry"
    assert destination["inputSerialization"]["type"] == "Json"

    activator_destination = next(
        (d for d in eventstream["destinations"] if d["type"] == "Activator"), None
    )
    assert activator_destination is not None, (
        "ES_MachineTelemetry has no Activator destination, so ACT_MachineHealth never "
        "receives events and can never alert. Re-run deploy.py."
    )
    assert activator_destination["properties"]["itemId"] == state["activator"]["id"], (
        "The Activator destination points at a different item than ACT_MachineHealth."
    )

    dashboard = decode_part(
        get_definition(workspace_id, state["dashboard"]["id"]),
        "RealTimeDashboard.json",
    )
    assert dashboard["schema_version"] == 77
    assert dashboard["flavor"] == "RTDashboard_Regular"
    assert len(dashboard["tiles"]) == 4
    assert dashboard["changeDetection"]["minRefreshRate"] == "10s"
    assert dashboard["changeDetection"]["fallbackRefreshRate"] == "30s"
    assert dashboard["dataSources"][0]["kind"] == "manual-kusto"
    assert dashboard["dataSources"][0]["clusterUri"]
    assert {parameter["displayName"] for parameter in dashboard["parameters"]} == {"Plant", "Line"}

    activator = decode_part(
        get_definition(workspace_id, state["activator"]["id"]),
        "ReflexEntities.json",
    )
    assert activator, "ACT_MachineHealth has no rule entities."
    entity_types = {entity["type"] for entity in activator}
    assert "eventstreamSource-v1" in entity_types
    source = next(e for e in activator if e["type"] == "eventstreamSource-v1")
    assert source["payload"]["metadata"]["eventstreamArtifactId"] == state["eventstream"]["id"]

    entity_ids = {entity["uniqueIdentifier"] for entity in activator}
    event_entity = next(
        e for e in activator
        if e["payload"].get("definition", {}).get("type") == "Event"
    )
    event_source_ref = json.loads(
        event_entity["payload"]["definition"]["instance"]
    )["steps"][0]["rows"][0]["arguments"][0]["value"]
    assert event_source_ref in entity_ids, (
        "The Activator event points at a stream source that no longer exists, so the "
        "rule receives nothing. Re-run deploy.py."
    )
    assert event_source_ref == source["uniqueIdentifier"], (
        "The Activator event is not bound to the live eventstream source."
    )
    rule = next(
        entity for entity in activator
        if entity["payload"].get("definition", {}).get("type") == "Rule"
    )
    assert rule["payload"]["definition"]["settings"]["shouldRun"] is True
    rule_instance = json.loads(rule["payload"]["definition"]["instance"])
    rule_rows = [row for step in rule_instance["steps"] for row in step["rows"]]
    threshold = next(row for row in rule_rows if row["kind"] == "NumberBecomes")
    assert {"name": "op", "type": "string", "value": "BecomesGreaterThan"} in threshold["arguments"]
    assert {"name": "value", "type": "number", "value": 8.0} in threshold["arguments"]
    email = next((row for row in rule_rows if row["kind"] == "EmailMessage"), None)
    fabric_action = next(
        (row for row in rule_rows if row["kind"] == "FabricItemInvocation"), None
    )
    assert email or fabric_action, (
        "The Activator rule has no action. Set it to Run Notebook -> NB_MaintenanceAction."
    )
    if email:
        recipients = next(arg for arg in email["arguments"] if arg["name"] == "sentTo")
        assert {"type": "string", "value": ALERT_RECIPIENT} in recipients["values"]
        action_summary = f"emails {ALERT_RECIPIENT} (no mailbox in this tenant)"
    else:
        action_summary = "runs NB_MaintenanceAction, which emails via ACS"

    agent = decode_part(
        get_definition(workspace_id, state["operationsAgent"]["id"]),
        "Configurations.json",
    )
    agent_config = agent["configuration"]
    assert "MachineTelemetry" in agent_config["instructions"]
    assert any(
        source["type"] == "KustoDatabase" and source["id"] == state["kqlDatabase"]["id"]
        for source in agent_config["dataSources"].values()
    )
    agent_actions = agent_config.get("actions") or {}
    assert agent_actions, (
        "OA_FactoryOperations has no action configured. Create its email action in the "
        "Fabric portal so the agent can recommend a maintenance response."
    )
    action_names = sorted(action.get("displayName", "") for action in agent_actions.values())
    for action in agent_actions.values():
        connection = action.get("connection") or {}
        if action.get("kind") == "FabricJobAction":
            assert connection.get("jobWorkspaceId") == workspace_id, (
                f"Operations Agent action '{action.get('displayName')}' targets workspace "
                f"{connection.get('jobWorkspaceId')}, outside this solution. Re-run deploy.py."
            )
    assert agent.get("shouldRun") is True, (
        "OA_FactoryOperations is stopped, so it will never evaluate telemetry. "
        "Run preflight.py to re-arm it."
    )
    agent_recipient = (agent_config.get("messageDestination") or {}).get("recipient")
    assert agent_recipient, "OA_FactoryOperations has no notification recipient configured."

    action_notebook = decode_text_part(
        get_definition(workspace_id, state["maintenanceNotebook"]["id"]),
        "notebook-content.py",
    )
    assert "__ACS_KEY__" not in action_notebook, (
        "NB_MaintenanceAction still has placeholder credentials. Re-run deploy.py."
    )
    mail_to = next(
        line.split('"')[1]
        for line in action_notebook.splitlines()
        if line.startswith("MAIL_TO")
    )

    telemetry = query(
        state,
        "MachineTelemetry | summarize Count=count(), "
        "LatestEvent=max(timestamp), LatestIngestion=max(ingestion_time()), "
        "FreshRows=countif(timestamp > ago(5m) and ingestion_time() > ago(5m))",
    )[0]
    if args.require_fresh and not telemetry["FreshRows"]:
        raise RuntimeError(
            "No fresh telemetry reached MachineTelemetry in the last five minutes. "
            f"Latest event: {telemetry['LatestEvent']}; "
            f"latest ingestion: {telemetry['LatestIngestion']}. "
            "Copy new SAS credentials from the current Eventstream Custom Endpoint "
            "details pane and rerun the simulator."
        )
    anomaly_rows = query(
        state,
        ANOMALY_QUERY_HEADER
        + "let binned = MachineTelemetry\n"
        "    | where timestamp > ago(win)\n"
        "    | summarize vib = avg(vibrationMm), maxTemp = max(temperatureC), "
        "faults = countif(status == 'fault')\n"
        "        by machineId, plant, line, ts = bin(timestamp, step);\n"
        + ANOMALY_QUERY_BODY,
    )
    m007_anomaly_count = sum(row["machineId"] == "M-007" for row in anomaly_rows)
    if args.require_anomaly and not m007_anomaly_count:
        raise RuntimeError(
            "No M-007 anomaly was detected in the last 20 minutes. "
            "Run the anomaly simulator, wait for ingestion, and retry."
        )

    print("PASS: KQL table and mapping")
    print("PASS: Eventstream custom endpoint -> processed Eventhouse topology")
    print("PASS: Eventstream also routes to ACT_MachineHealth (Activator destination)")
    print("PASS: Dashboard schema v77 has four tiles, two filters, and 10-second refresh")
    print(f"PASS: Activator rule fires above 8.0 mm and {action_summary}")
    print(f"PASS: Operations Agent telemetry source and action(s): {', '.join(action_names)}")
    print(f"PASS: Operations Agent is running; notifies {agent_recipient}")
    print(f"PASS: NB_MaintenanceAction emails {mail_to} via Azure Communication Services")
    print(f"PASS: MachineTelemetry contains {telemetry['Count']} rows")
    if telemetry["FreshRows"]:
        print(f"PASS: Fresh telemetry rows in the last five minutes: {telemetry['FreshRows']}")
    else:
        print(
            "WARNING: No fresh telemetry in the last five minutes. "
            f"Latest event: {telemetry['LatestEvent']}; "
            f"latest ingestion: {telemetry['LatestIngestion']}."
        )
    if m007_anomaly_count:
        print(f"PASS: M-007 anomaly rows detected in the last 20 minutes: {m007_anomaly_count}")
    else:
        print("INFO: No M-007 anomaly detected in the last 20 minutes (not required).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
