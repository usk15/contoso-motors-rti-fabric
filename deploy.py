from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import requests


FABRIC_API = "https://api.fabric.microsoft.com/v1"
ROOT = Path(__file__).resolve().parent
NAMESPACE = uuid.UUID("d68cbd3c-cf19-49a1-a6c2-1647da4dd1de")


class DeploymentError(RuntimeError):
    pass


def state_path() -> Path:
    """Path of the deployment state file.

    Overridable with RTI_STATE_FILE so the same checkout can deploy to more
    than one tenant or subscription without the second deployment overwriting
    the first one's artifact IDs.
    """
    override = os.environ.get("RTI_STATE_FILE")
    if not override:
        return ROOT / "deployment-state.json"
    path = Path(override)
    return path if path.is_absolute() else ROOT / path


def stable_id(label: str) -> str:
    return str(uuid.uuid5(NAMESPACE, label))


def agent_recipient() -> str:
    """UPN that receives Operations Agent notifications.

    Fabric only accepts a recipient from the signed-in tenant, so this defaults to
    the deploying user and can be overridden with OPERATIONS_AGENT_RECIPIENT.
    """
    override = os.environ.get("OPERATIONS_AGENT_RECIPIENT")
    if override:
        return override
    account = subprocess.run(
        ["az", "account", "show", "--query", "user.name", "-o", "tsv"],
        capture_output=True,
        text=True,
        shell=True,
        check=False,
    )
    upn = account.stdout.strip()
    if not upn:
        raise DeploymentError("Unable to resolve the signed-in UPN for the Operations Agent.")
    return upn


def inline_part(path: str, value: str | dict[str, Any]) -> dict[str, str]:
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return {
        "path": path,
        "payload": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "payloadType": "InlineBase64",
    }


def platform(item_type: str, display_name: str, description: str) -> dict[str, Any]:
    return {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/"
            "gitIntegration/platformProperties/2.0.0/schema.json"
        ),
        "metadata": {
            "type": item_type,
            "displayName": display_name,
            "description": description,
        },
        "config": {"version": "2.0", "logicalId": stable_id(f"{item_type}:{display_name}")},
    }


CAPACITY_RESOURCE_ID = os.environ.get("FABRIC_CAPACITY_RESOURCE_ID")


def _az(args: list[str]) -> str:
    result = subprocess.run(
        ["az", *args],
        check=True,
        capture_output=True,
        text=True,
        shell=os.name == "nt",
    )
    return result.stdout.strip()


def _capacity_state() -> str:
    if not CAPACITY_RESOURCE_ID:
        raise DeploymentError(
            "Set FABRIC_CAPACITY_RESOURCE_ID to your Fabric capacity's Azure resource ID "
            "(/subscriptions/.../resourceGroups/.../providers/Microsoft.Fabric/capacities/...)."
        )
    return _az(
        ["resource", "show", "--ids", CAPACITY_RESOURCE_ID,
         "--query", "properties.state", "--output", "tsv"]
    )


def ensure_capacity_active(timeout: int = 600) -> None:
    """Resume the Fabric capacity if it is paused.

    A paused capacity makes every Fabric REST call fail with a confusing 404,
    so callers resume it up front rather than misreporting missing artifacts.
    An autopause runbook can suspend it at any time, so a transitional
    'Pausing' state must settle before the resume is accepted.
    """
    try:
        state = _capacity_state()
    except subprocess.CalledProcessError:
        return
    if state.lower() == "active":
        return
    deadline = time.time() + timeout
    while state.lower() in ("pausing", "resuming") and time.time() < deadline:
        print(f"Fabric capacity is {state}; waiting for it to settle...")
        time.sleep(15)
        state = _capacity_state()
        if state.lower() == "active":
            print("Fabric capacity is Active.")
            return
    print(f"Fabric capacity is {state}; resuming...")
    for attempt in range(5):
        try:
            _az(["resource", "invoke-action", "--ids", CAPACITY_RESOURCE_ID, "--action", "resume"])
            break
        except subprocess.CalledProcessError as error:
            if attempt == 4:
                raise DeploymentError(
                    f"Could not resume the Fabric capacity: {error.stderr}"
                ) from error
            print("  Capacity is not ready to be updated; retrying...")
            time.sleep(20)
    while time.time() < deadline:
        if _capacity_state().lower() == "active":
            print("Fabric capacity is Active.")
            return
        time.sleep(10)
    raise DeploymentError("Timed out waiting for the Fabric capacity to become Active.")


def get_token(resource: str = "https://api.fabric.microsoft.com") -> str:
    result = subprocess.run(
        [
            "az", "account", "get-access-token", "--resource", resource,
            "--query", "accessToken", "--output", "tsv",
        ],
        check=True,
        capture_output=True,
        text=True,
        shell=os.name == "nt",
    )
    token = result.stdout.strip()
    if not token:
        raise DeploymentError(f"Azure CLI returned an empty token for {resource}.")
    return token


class FabricClient:
    def __init__(self) -> None:
        self.session = requests.Session()

    def _headers(self, url: str) -> dict[str, str]:
        resource = (
            "https://analysis.windows.net/powerbi/api"
            if "analysis.windows.net" in url
            else "https://api.fabric.microsoft.com"
        )
        return {"Authorization": f"Bearer {get_token(resource)}", "Content-Type": "application/json"}

    def request(
        self,
        method: str,
        path_or_url: str,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200, 201, 202),
    ) -> requests.Response:
        url = path_or_url if path_or_url.startswith("http") else f"{FABRIC_API}{path_or_url}"
        for attempt in range(1, 5):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self._headers(url),
                    json=payload,
                    timeout=90,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if attempt == 4:
                    raise DeploymentError(f"{method} {url} failed: {exc}") from exc
                time.sleep(attempt * 5)
                continue
            if response.status_code in expected:
                return response
            name_releasing = (
                response.status_code == 409
                and "ItemDisplayNameNotAvailableYet" in response.text
            )
            if response.status_code in (429, 500, 502, 503, 504) or name_releasing:
                time.sleep(int(response.headers.get("Retry-After", attempt * 10)))
                continue
            raise DeploymentError(
                f"{method} {url} failed ({response.status_code}): {response.text}"
            )
        raise DeploymentError(f"{method} {url} failed after retries: {response.text}")

    def complete(self, response: requests.Response) -> dict[str, Any]:
        if response.status_code in (200, 201):
            return response.json() if response.content else {}
        location = response.headers.get("Location")
        operation_id = response.headers.get("x-ms-operation-id")
        if not location and operation_id:
            location = f"{FABRIC_API}/operations/{operation_id}"
        if not location:
            return {}
        deadline = time.time() + 900
        while time.time() < deadline:
            time.sleep(min(int(response.headers.get("Retry-After", "5")), 15))
            poll = self.request("GET", location, expected=(200,))
            body = poll.json() if poll.content else {}
            status = body.get("status")
            if status in ("Succeeded", "Completed"):
                result_url = location.rstrip("/") + "/result"
                result = self.request("GET", result_url, expected=(200, 404))
                return result.json() if result.status_code == 200 and result.content else body
            if status in ("Failed", "Cancelled"):
                raise DeploymentError(f"Fabric operation failed: {json.dumps(body)}")
        raise DeploymentError(f"Timed out waiting for operation {location}.")

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self.request("GET", "/workspaces", expected=(200,)).json().get("value", [])

    def list_items(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.request(
            "GET", f"/workspaces/{workspace_id}/items", expected=(200,)
        ).json().get("value", [])


def find_named(items: list[dict[str, Any]], display_name: str, item_type: str) -> dict[str, Any] | None:
    return next(
        (
            item for item in items
            if item.get("displayName") == display_name and item.get("type") == item_type
        ),
        None,
    )


def wait_for_named(
    client: FabricClient,
    workspace_id: str,
    display_name: str,
    item_type: str,
) -> dict[str, Any]:
    deadline = time.time() + 180
    while time.time() < deadline:
        found = find_named(client.list_items(workspace_id), display_name, item_type)
        if found:
            return found
        time.sleep(5)
    raise DeploymentError(f"{item_type} '{display_name}' wasn't visible after creation.")


def ensure_workspace(client: FabricClient, capacity_id: str) -> dict[str, Any]:
    existing = next(
        (workspace for workspace in client.list_workspaces()
         if workspace.get("displayName") == "Contoso-Motors-Demo"),
        None,
    )
    if existing:
        if existing.get("capacityId") != capacity_id:
            client.complete(
                client.request(
                    "POST",
                    f"/workspaces/{existing['id']}/assignToCapacity",
                    {"capacityId": capacity_id},
                )
            )
        return existing
    response = client.request(
        "POST",
        "/workspaces",
        {
            "displayName": "Contoso-Motors-Demo",
            "description": "Contoso Motors real-time factory operations demonstration.",
            "capacityId": capacity_id,
        },
    )
    client.complete(response)
    return next(
        workspace for workspace in client.list_workspaces()
        if workspace.get("displayName") == "Contoso-Motors-Demo"
    )


def ensure_eventhouse(client: FabricClient, workspace_id: str) -> dict[str, Any]:
    existing = find_named(client.list_items(workspace_id), "EH_ContosoMotors", "Eventhouse")
    if existing:
        return existing
    response = client.request(
        "POST",
        f"/workspaces/{workspace_id}/eventhouses",
        {
            "displayName": "EH_ContosoMotors",
            "description": "Eventhouse for live Contoso Motors machine telemetry.",
        },
    )
    result = client.complete(response)
    return result if result.get("id") else wait_for_named(
        client, workspace_id, "EH_ContosoMotors", "Eventhouse"
    )


def ensure_kql_database(
    client: FabricClient,
    workspace_id: str,
    eventhouse_id: str,
) -> dict[str, Any]:
    existing = find_named(client.list_items(workspace_id), "ContosoTelemetry", "KQLDatabase")
    properties = {
        "databaseType": "ReadWrite",
        "parentEventhouseItemId": eventhouse_id,
        "oneLakeCachingPeriod": "P36500D",
        "oneLakeStandardStoragePeriod": "P36500D",
    }
    schema = (ROOT / "kql" / "schema.kql").read_text(encoding="utf-8")
    definition = {
        "parts": [
            inline_part("DatabaseProperties.json", properties),
            inline_part("DatabaseSchema.kql", schema),
            inline_part(
                ".platform",
                platform("KQLDatabase", "ContosoTelemetry", "Contoso machine telemetry database."),
            ),
        ]
    }
    if existing:
        client.complete(
            client.request(
                "POST",
                f"/workspaces/{workspace_id}/items/{existing['id']}/updateDefinition",
                {"definition": definition},
            )
        )
        return existing
    response = client.request(
        "POST",
        f"/workspaces/{workspace_id}/kqlDatabases",
        {
            "displayName": "ContosoTelemetry",
            "description": "Contoso machine telemetry database.",
            "definition": definition,
        },
    )
    result = client.complete(response)
    return result if result.get("id") else wait_for_named(
        client, workspace_id, "ContosoTelemetry", "KQLDatabase"
    )


def current_definition(
    client: FabricClient,
    workspace_id: str,
    item_id: str,
) -> dict[str, str] | None:
    try:
        response = client.request(
            "POST", f"/workspaces/{workspace_id}/items/{item_id}/getDefinition"
        )
        body = client.complete(response)
    except DeploymentError:
        return None
    parts = body.get("definition", {}).get("parts", [])
    return {part["path"]: part.get("payload", "") for part in parts}


def _strip_server_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_server_fields(item)
            for key, item in value.items()
            if key != "id"
        }
    if isinstance(value, list):
        return [_strip_server_fields(item) for item in value]
    return value


def _payload_matches(deployed_payload: str, desired_payload: str) -> bool:
    if deployed_payload == desired_payload:
        return True
    try:
        deployed = json.loads(base64.b64decode(deployed_payload).decode("utf-8"))
        desired = json.loads(base64.b64decode(desired_payload).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return False
    return _strip_server_fields(deployed) == _strip_server_fields(desired)


def ensure_generic_item(
    client: FabricClient,
    workspace_id: str,
    display_name: str,
    item_type: str,
    description: str,
    parts: list[dict[str, str]],
    definition_format: str | None = None,
    skip_if_unchanged: bool = False,
) -> dict[str, Any]:
    existing = find_named(client.list_items(workspace_id), display_name, item_type)
    definition: dict[str, Any] = {"parts": parts}
    if definition_format:
        definition["format"] = definition_format
    if existing:
        if skip_if_unchanged:
            deployed = current_definition(client, workspace_id, existing["id"])
            desired = {part["path"]: part["payload"] for part in parts}
            if deployed is not None and all(
                _payload_matches(deployed.get(path, ""), payload)
                for path, payload in desired.items()
                if path != ".platform"
            ):
                return existing
        response = client.request(
            "POST",
            f"/workspaces/{workspace_id}/items/{existing['id']}/updateDefinition",
            {"definition": definition},
        )
        client.complete(response)
        return existing
    response = client.request(
        "POST",
        f"/workspaces/{workspace_id}/items",
        {
            "displayName": display_name,
            "type": item_type,
            "description": description,
            "definition": definition,
        },
    )
    result = client.complete(response)
    return result if result.get("id") else wait_for_named(
        client, workspace_id, display_name, item_type
    )


def wait_for_eventstream_ready(
    client: FabricClient,
    workspace_id: str,
    eventstream_id: str,
    timeout: int = 300,
) -> None:
    """Wait until no eventstream node is mid-transition.

    Updating the topology while a node is 'Creating' fails with
    DataSourcesValidationError.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            topology = client.request(
                "GET",
                f"/workspaces/{workspace_id}/eventstreams/{eventstream_id}/topology",
                expected=(200,),
            ).json()
        except DeploymentError:
            return
        states = [
            node.get("status")
            for group in ("sources", "destinations", "streams")
            for node in topology.get(group, [])
        ]
        if all(state not in ("Creating", "Updating", "Deleting", "Resuming") for state in states):
            return
        time.sleep(10)


def ensure_eventstream(
    client: FabricClient,
    workspace_id: str,
    database_id: str,
    activator_id: str | None = None,
) -> dict[str, Any]:
    destinations = [
        {
            "name": "MachineTelemetryEventhouse",
            "type": "Eventhouse",
            "properties": {
                "dataIngestionMode": "ProcessedIngestion",
                "workspaceId": workspace_id,
                "itemId": database_id,
                "databaseName": "ContosoTelemetry",
                "tableName": "MachineTelemetry",
                "inputSerialization": {
                    "type": "Json",
                    "properties": {"encoding": "UTF8"},
                },
            },
            "inputNodes": [{"name": "MachineTelemetryStream"}],
        }
    ]
    if activator_id:
        # Without this destination the Activator receives no events, so its
        # rule never evaluates and no alert is ever sent.
        destinations.append(
            {
                "name": "MachineTelemetryActivator",
                "type": "Activator",
                "properties": {
                    "workspaceId": workspace_id,
                    "itemId": activator_id,
                    "inputSerialization": {
                        "type": "Json",
                        "properties": {"encoding": "UTF8"},
                    },
                },
                "inputNodes": [{"name": "MachineTelemetryStream"}],
            }
        )
    topology = {
        "sources": [
            {
                "name": "MachineTelemetryCustomEndpoint",
                "type": "CustomEndpoint",
                "properties": {},
            }
        ],
        "destinations": destinations,
        "streams": [
            {
                "name": "MachineTelemetryStream",
                "type": "DefaultStream",
                "properties": {},
                "inputNodes": [{"name": "MachineTelemetryCustomEndpoint"}],
            }
        ],
        "operators": [],
        "compatibilityLevel": "1.1",
    }
    return ensure_generic_item(
        client,
        workspace_id,
        "ES_MachineTelemetry",
        "Eventstream",
        "Custom endpoint to Eventhouse processed-ingestion stream.",
        [
            inline_part("eventstream.json", topology),
            inline_part(
                "eventstreamProperties.json",
                {"retentionTimeInDays": 1, "eventThroughputLevel": "Low"},
            ),
            inline_part(
                ".platform",
                platform(
                    "Eventstream",
                    "ES_MachineTelemetry",
                    "Custom endpoint to Eventhouse processed-ingestion stream.",
                ),
            ),
        ],
        "eventstream",
        skip_if_unchanged=True,
    )


def notebook_definition() -> dict[str, Any]:
    source = (ROOT / "simulator.py").read_text(encoding="utf-8")
    return {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# Contoso Motors telemetry simulator\n",
                    "Set `EVENTSTREAM_CONN_STR` and `EVENTSTREAM_EH_NAME` in the notebook environment, "
                    "then execute the Python cell.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [line + "\n" for line in source.splitlines()],
            },
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.12"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def ensure_notebook(client: FabricClient, workspace_id: str) -> dict[str, Any]:
    return ensure_generic_item(
        client,
        workspace_id,
        "NB_TelemetrySimulator",
        "Notebook",
        "Re-runnable Python machine telemetry simulator.",
        [
            inline_part("notebook-content.ipynb", notebook_definition()),
            inline_part(
                ".platform",
                platform("Notebook", "NB_TelemetrySimulator", "Machine telemetry simulator."),
            ),
        ],
        "ipynb",
    )


def maintenance_notebook_definition() -> dict[str, Any]:
    params = '''# Parameters supplied by the calling Activator rule or Operations Agent.
machineId = ""
line = ""
plant = ""
vibrationMm = ""
timestamp = ""
raisedBy = ""
'''
    code = '''# Contoso Motors - Operations Agent maintenance action.
# The Operations Agent runs this notebook when a machine crosses the
# high-vibration threshold. It emails maintenance and records an audit row.
import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import requests
import notebookutils

CLUSTER_URI = "__CLUSTER_URI__"
DATABASE = "__DATABASE__"
ACS_ENDPOINT = "__ACS_ENDPOINT__"
ACS_KEY = "__ACS_KEY__"
ACS_SENDER = "__ACS_SENDER__"
MAIL_TO = "__MAIL_TO__"


def as_float(value):
    """Activator sends parameters as text, and sends none on a test run."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


machineId = str(machineId or "").strip()
vibrationMm = as_float(vibrationMm)
# Only the Activator can map parameters, so it passes raisedBy explicitly.
# The Operations Agent has no parameter mapping UI, so it falls through to the default.
raisedBy = str(raisedBy or "").strip() or "OA_FactoryOperations (Operations Agent)"

notes = f"Vibration {vibrationMm} mm exceeded the 8.0 mm maintenance threshold."


def kusto(csl):
    token = notebookutils.credentials.getToken("kusto")
    response = requests.post(
        f"{CLUSTER_URI}/v1/rest/query",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"db": DATABASE, "csl": csl},
        timeout=120,
    )
    response.raise_for_status()
    table = response.json()["Tables"][0]
    columns = [c["ColumnName"] for c in table["Columns"]]
    return [dict(zip(columns, row)) for row in table["Rows"]]


if not machineId:
    # The Activator's FabricItemInvocation action passes no parameters, so look
    # up the machine that actually breached the threshold.
    hits = kusto(
        "MachineTelemetry "
        "| where timestamp > ago(10m) and vibrationMm > 8.0 "
        "| summarize arg_max(timestamp, machineId, line, plant, vibrationMm) by machineId "
        "| top 1 by vibrationMm desc"
    )
    if not hits:
        print("No machine above 8.0 mm in the last 10 minutes; nothing to report.")
        raise SystemExit(0)
    hit = hits[0]
    machineId = hit["machineId"]
    line = hit["line"]
    plant = hit["plant"]
    vibrationMm = hit["vibrationMm"]
    timestamp = str(hit["timestamp"])
    notes = f"Vibration {vibrationMm} mm exceeded the 8.0 mm maintenance threshold."
    print(f"Resolved breaching machine from telemetry: {machineId}")


def send_email(subject, text):
    """Azure Communication Services email, signed with the access key."""
    host = ACS_ENDPOINT.replace("https://", "").rstrip("/")
    path = "/emails:send?api-version=2023-03-31"
    body = json.dumps(
        {
            "senderAddress": ACS_SENDER,
            "recipients": {"to": [{"address": MAIL_TO}]},
            "content": {"subject": subject, "plainText": text},
        },
        separators=(",", ":"),
    )
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    signature = base64.b64encode(
        hmac.new(
            base64.b64decode(ACS_KEY),
            f"POST\\n{path}\\n{date};{host};{digest}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode()
    headers = {
        "Content-Type": "application/json",
        "x-ms-date": date,
        "x-ms-content-sha256": digest,
        "Authorization": (
            "HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256"
            f"&Signature={signature}"
        ),
        "repeatability-request-id": str(uuid.uuid4()),
        "repeatability-first-sent": date,
    }
    response = requests.post(
        f"https://{host}{path}", headers=headers, data=body, timeout=90
    )
    response.raise_for_status()
    return response.json().get("id")


detail = (
    "Streaming rule ACT_MachineHealth evaluated this event within seconds of arrival."
    if "Activator" in raisedBy
    else "Operations Agent OA_FactoryOperations identified this condition and the "
         "recommended action was approved by an operator."
)
subject = f"Predictive maintenance required - {machineId} [{raisedBy}]"
text = (
    f"Machine: {machineId}\\n"
    f"Line: {line}\\n"
    f"Plant: {plant}\\n"
    f"Vibration: {vibrationMm} mm\\n"
    f"Event time (UTC): {timestamp}\\n\\n"
    f"{notes}\\n\\n"
    f"Detected by: {raisedBy}\\n"
    f"{detail}\\n\\n"
    "Contoso Motors - Microsoft Fabric Real-Time Intelligence."
)
operation_id = send_email(subject, text)
print(f"Maintenance email queued for {MAIL_TO} (operation {operation_id}).")

row = {
    "requestedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "machineId": machineId,
    "line": line,
    "plant": plant,
    "vibrationMm": float(vibrationMm or 0.0),
    "eventTimestamp": timestamp,
    "requestedBy": raisedBy,
    "notes": notes,
}

token = notebookutils.credentials.getToken("kusto")
payload = json.dumps(row, separators=(",", ":"))
ingest = requests.post(
    f"{CLUSTER_URI}/v1/rest/mgmt",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json={
        "db": DATABASE,
        "csl": (
            ".ingest inline into table MaintenanceRequests "
            "with (format='multijson') <|\\n" + payload
        ),
    },
    timeout=120,
)
ingest.raise_for_status()

print(f"Maintenance request recorded for {machineId}: {notes}")
'''
    return {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# Maintenance action\n",
                    "Invoked by `OA_FactoryOperations` when a machine crosses "
                    "the 8.0 mm vibration threshold. Emails maintenance and writes "
                    "an auditable row to the `MaintenanceRequests` table.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {"tags": ["parameters"]},
                "outputs": [],
                "source": [text + "\n" for text in params.splitlines()],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [text + "\n" for text in code.splitlines()],
            },
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.12"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def acs_settings() -> dict[str, str]:
    """Resolve ACS email settings at deploy time.

    The access key is fetched from Azure and injected into the deployed Fabric
    notebook, so it never lives in this repository. Configure your own
    Communication Services resource via ACS_NAME / ACS_RESOURCE_GROUP, or
    supply ACS_CONNECTION_STRING directly. See README-RTI.md for setup.
    """
    connection = os.environ.get("ACS_CONNECTION_STRING")
    if not connection:
        name = os.environ.get("ACS_NAME")
        group = os.environ.get("ACS_RESOURCE_GROUP")
        if not name or not group:
            raise DeploymentError(
                "Set ACS_NAME and ACS_RESOURCE_GROUP (or ACS_CONNECTION_STRING) to "
                "your own Azure Communication Services resource before deploying."
            )
        result = subprocess.run(
            ["az", "communication", "list-key", "--name", name,
             "--resource-group", group, "-o", "json"],
            capture_output=True,
            text=True,
            shell=True,
            check=False,
        )
        if result.returncode != 0:
            raise DeploymentError(
                f"Unable to read ACS keys for {name} in {group}: {result.stderr.strip()[:200]}"
            )
        connection = json.loads(result.stdout)["primaryConnectionString"]
    try:
        endpoint = connection.split("endpoint=")[1].split(";")[0].rstrip("/")
        key = connection.split("accesskey=")[1].split(";")[0]
    except IndexError as exc:
        raise DeploymentError("ACS connection string is malformed.") from exc
    sender = os.environ.get("ACS_SENDER_ADDRESS")
    if not sender:
        raise DeploymentError(
            "Set ACS_SENDER_ADDRESS to a verified sender on your Communication "
            "Services email domain (see README-RTI.md)."
        )
    return {
        "endpoint": endpoint,
        "key": key,
        "sender": sender,
        "mailTo": os.environ.get("MAINTENANCE_EMAIL_TO", "maintenance-team@example.com"),
    }


def ensure_maintenance_notebook(
    client: FabricClient,
    workspace_id: str,
    cluster_uri: str,
) -> dict[str, Any]:
    acs = acs_settings()
    definition = json.loads(
        json.dumps(maintenance_notebook_definition())
        .replace("__CLUSTER_URI__", cluster_uri)
        .replace("__DATABASE__", "ContosoTelemetry")
        .replace("__ACS_ENDPOINT__", acs["endpoint"])
        .replace("__ACS_KEY__", acs["key"])
        .replace("__ACS_SENDER__", acs["sender"])
        .replace("__MAIL_TO__", acs["mailTo"])
    )
    return ensure_generic_item(
        client,
        workspace_id,
        "NB_MaintenanceAction",
        "Notebook",
        "Operations Agent action: email maintenance and record the request.",
        [
            inline_part("notebook-content.ipynb", definition),
            inline_part(
                ".platform",
                platform(
                    "Notebook",
                    "NB_MaintenanceAction",
                    "Operations Agent maintenance action.",
                ),
            ),
        ],
        "ipynb",
    )


def query_texts() -> list[tuple[str, str]]:
    return [
        (
            "Live vibration trend",
            "MachineTelemetry\n| where timestamp > ago(5m)\n"
            "| summarize avgVib = avg(vibrationMm) by machineId, bin(timestamp, 10s)\n"
            "| render timechart",
        ),
        (
            "Active anomalies",
            ANOMALY_QUERY_HEADER
            + "let binned = MachineTelemetry\n"
            "    | where timestamp > ago(win)\n"
            "    | summarize vib = avg(vibrationMm), maxTemp = max(temperatureC), "
            "faults = countif(status == 'fault')\n"
            "        by machineId, plant, line, ts = bin(timestamp, step);\n"
            + ANOMALY_QUERY_BODY,
        ),
        (
            "Live throughput",
            "MachineTelemetry\n| where timestamp > ago(5m)\n"
            "| summarize throughputUnits = sum(throughputUnits) by line",
        ),
        (
            "Fault machine count",
            "MachineTelemetry\n| where timestamp > ago(2m)\n"
            "| summarize arg_max(timestamp, status) by machineId\n"
            '| where status == "fault"\n| count',
        ),
    ]


def ensure_queryset(
    client: FabricClient,
    workspace_id: str,
    cluster_uri: str,
    database_id: str,
) -> dict[str, Any]:
    source_id = stable_id("queryset:source")
    queryset = {
        "queryset": {
            "version": "1.0.0",
            "dataSources": [
                {
                    "id": source_id,
                    "clusterUri": cluster_uri,
                    "type": "AzureDataExplorer",
                    "databaseName": database_id,
                }
            ],
            "tabs": [
                {
                    "id": stable_id(f"queryset:{title}"),
                    "content": query,
                    "title": title,
                    "dataSourceId": source_id,
                }
                for title, query in query_texts()
            ],
        }
    }
    return ensure_generic_item(
        client,
        workspace_id,
        "KQLQ_FactoryAnalytics",
        "KQLQueryset",
        "Saved trend, anomaly, throughput, and fault queries.",
        [
            inline_part("RealTimeQueryset.json", queryset),
            inline_part(
                ".platform",
                platform(
                    "KQLQueryset",
                    "KQLQ_FactoryAnalytics",
                    "Saved factory telemetry queries.",
                ),
            ),
        ],
    )


def chart_options(x_column: str, y_columns: list[str], series: list[str] | None = None) -> dict[str, Any]:
    return {
        "multipleYAxes": {
            "base": {
                "id": "-1", "label": "", "columns": [], "yAxisMaximumValue": None,
                "yAxisMinimumValue": None, "yAxisScale": "linear", "horizontalLines": [],
            },
            "additional": [],
            "showMultiplePanels": False,
        },
        "hideLegend": False,
        "legendLocation": "bottom",
        "xColumnTitle": "",
        "xColumn": x_column,
        "yColumns": y_columns,
        "seriesColumns": series or [],
        "xAxisScale": "linear",
        "verticalLine": "",
        "crossFilterDisabled": False,
        "drillthroughDisabled": False,
        "crossFilter": [],
        "drillthrough": [],
    }


def ensure_dashboard(
    client: FabricClient,
    workspace_id: str,
    cluster_uri: str,
    database_id: str,
) -> dict[str, Any]:
    page_id = stable_id("dashboard:page")
    data_source_id = stable_id("dashboard:source")
    dashboard_queries = [
        (
            "throughput",
            "MachineTelemetry | where timestamp > ago(5m) "
            "| where isempty(_plant) or plant in (_plant) "
            "| where isempty(_line) or line in (_line) "
            "| summarize throughputUnits=sum(throughputUnits) by line",
        ),
        (
            "vibration",
            "MachineTelemetry | where timestamp > ago(5m) "
            "| where isempty(_plant) or plant in (_plant) "
            "| where isempty(_line) or line in (_line) "
            "| summarize avgVib=avg(vibrationMm) by machineId, bin(timestamp, 10s)",
        ),
        (
            "anomalies",
            ANOMALY_QUERY_HEADER
            + "let binned = MachineTelemetry\n"
            "    | where timestamp > ago(win)\n"
            "    | summarize vib = avg(vibrationMm), maxTemp = max(temperatureC), "
            "faults = countif(status == 'fault')\n"
            "        by machineId, plant, line, ts = bin(timestamp, step);\n"
            + ANOMALY_QUERY_BODY
            + "\n| where isempty(_plant) or plant in (_plant)"
            "\n| where isempty(_line) or line in (_line)",
        ),
        (
            "faults",
            "MachineTelemetry | where timestamp > ago(2m) "
            "| where isempty(_plant) or plant in (_plant) "
            "| where isempty(_line) or line in (_line) "
            "| summarize arg_max(timestamp,status) by machineId | where status == 'fault' "
            "| summarize Metric='Fault machines', Value=count()",
        ),
        ("plant-filter", "MachineTelemetry | distinct value=plant | order by value asc"),
        ("line-filter", "MachineTelemetry | distinct value=line | order by value asc"),
    ]
    queries = [
        {
            "dataSource": {"kind": "inline", "dataSourceId": data_source_id},
            "text": text,
            "id": stable_id(f"dashboard:query:{name}"),
            "usedVariables": (
                [] if name.endswith("-filter") else ["_plant", "_line"]
            ),
        }
        for name, text in dashboard_queries
    ]

    def tile(
        name: str,
        title: str,
        visual_type: str,
        x: int,
        y: int,
        width: int,
        height: int,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "id": stable_id(f"dashboard:tile:{name}"),
            "title": title,
            "visualType": visual_type,
            "pageId": page_id,
            "layout": {"x": x, "y": y, "width": width, "height": height},
            "queryRef": {"kind": "query", "queryId": stable_id(f"dashboard:query:{name}")},
            "visualOptions": options,
        }

    table_options = {
        "table__enableRenderLinks": True,
        "colorRulesDisabled": True,
        "colorStyle": "light",
        "crossFilterDisabled": False,
        "drillthroughDisabled": False,
        "crossFilter": [],
        "drillthrough": [],
        "table__renderLinks": [],
        "colorRules": [],
        "selectedDataOnLoad": {"all": True, "limit": 30},
    }
    stat_options = {
        "multiStat__textSize": "auto",
        "multiStat__valueColumn": "Value",
        "multiStat__labelColumn": "Metric",
        "multiStat__displayOrientation": "horizontal",
        "multiStat__slot": {"width": 3, "height": 1},
        "colorRulesDisabled": True,
        "colorStyle": "light",
        "colorRules": [],
        "crossFilter": [],
        "drillthrough": [],
        "crossFilterDisabled": False,
        "drillthroughDisabled": False,
    }
    parameters = [
        {
            "kind": "string",
            "id": stable_id("dashboard:parameter:plant"),
            "displayName": "Plant",
            "description": "Filter tiles by plant.",
            "variableName": "_plant",
            "selectionType": "array",
            "includeAllOption": True,
            "defaultValue": {"kind": "all"},
            "dataSource": {
                "kind": "query",
                "columns": {"value": "value"},
                "queryRef": {
                    "kind": "query",
                    "queryId": stable_id("dashboard:query:plant-filter"),
                },
            },
            "showOnPages": {"kind": "all"},
            "allIsNull": True,
        },
        {
            "kind": "string",
            "id": stable_id("dashboard:parameter:line"),
            "displayName": "Line",
            "description": "Filter tiles by production line.",
            "variableName": "_line",
            "selectionType": "array",
            "includeAllOption": True,
            "defaultValue": {"kind": "all"},
            "dataSource": {
                "kind": "query",
                "columns": {"value": "value"},
                "queryRef": {
                    "kind": "query",
                    "queryId": stable_id("dashboard:query:line-filter"),
                },
            },
            "showOnPages": {"kind": "all"},
            "allIsNull": True,
        },
    ]
    dashboard = {
        "schema_version": 77,
        "flavor": "RTDashboard_Regular",
        "changeDetection": {
            "kind": "liveUpdates",
            "fallbackRefreshRate": "30s",
            "minRefreshRate": "10s",
        },
        "title": "Contoso Motors Factory Floor",
        "baseQueries": [],
        "tiles": [
            tile(
                "throughput", "Live throughput by line", "bar", 0, 0, 12, 7,
                chart_options("line", ["throughputUnits"]),
            ),
            tile(
                "vibration", "Five-minute vibration trend", "line", 12, 0, 12, 7,
                chart_options("timestamp", ["avgVib"], ["machineId"]),
            ),
            tile("anomalies", "Active anomalies", "table", 0, 7, 18, 8, table_options),
            tile("faults", "Fault machines", "multistat", 18, 7, 6, 8, stat_options),
        ],
        "dataSources": [
            {
                "id": data_source_id,
                "name": "ContosoTelemetry",
                "clusterUri": cluster_uri,
                "database": database_id,
                "kind": "manual-kusto",
            }
        ],
        "pages": [{"name": "Factory Floor", "id": page_id}],
        "parameters": parameters,
        "queries": queries,
        "embeddedApps": [],
    }
    return ensure_generic_item(
        client,
        workspace_id,
        "RTD_FactoryFloor",
        "KQLDashboard",
        "Live Contoso Motors factory operations dashboard.",
        [
            inline_part("RealTimeDashboard.json", dashboard),
            inline_part(
                ".platform",
                platform(
                    "KQLDashboard",
                    "RTD_FactoryFloor",
                    "Live Contoso Motors factory operations dashboard.",
                ),
            ),
        ],
    )


ALERT_RECIPIENT = os.environ.get("MAINTENANCE_EMAIL_TO", "maintenance-team@example.com")

# Peer-deviation anomaly detector. A machine is anomalous when its 10s average
# vibration breaches the absolute damage threshold AND sits far off the fleet
# median for the same bucket. Peer comparison stays valid on short or bursty
# windows, unlike a per-series baseline fit which needs a long dense history.
ANOMALY_QUERY_HEADER = (
    "let win = 20m;\n"
    "let step = 10s;\n"
    "let minVib = 8.0;\n"
    "let minScore = 3.0;\n"
)

ANOMALY_QUERY_BODY = (
    "let fleet = binned\n"
    "    | summarize fleetMedian = percentile(vib, 50), fleetStd = stdev(vib) by ts;\n"
    "binned\n"
    "| join kind=inner fleet on ts\n"
    "| extend score = iff(fleetStd > 0.0, (vib - fleetMedian) / fleetStd, 0.0)\n"
    "| where vib > minVib and score > minScore\n"
    "| project timestamp = ts, machineId, plant, line, vib = round(vib, 2), "
    "fleetMedian = round(fleetMedian, 2), score = round(score, 2), maxTemp, faults\n"
    "| order by timestamp desc"
)



def reflex_entities(
    eventstream_id: str,
    live_source_id: str | None = None,
    action_notebook_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build the Activator entity graph.

    When Fabric creates the Activator destination on the eventstream it injects
    its own eventstreamSource entity, and that is the one that actually receives
    events. Pass its id as live_source_id so the rule reads the live stream
    rather than the hand-authored placeholder source.
    """
    container_id = stable_id("reflex:container")
    source_id = live_source_id or stable_id("reflex:stream-source")
    event_id = stable_id("reflex:event")
    object_id = stable_id("reflex:object")
    identity_id = stable_id("reflex:attribute:machineId")
    vibration_id = stable_id("reflex:attribute:vibrationMm")
    rule_id = stable_id("reflex:rule:high-vibration")
    tuple_id = stable_id("reflex:attribute:machineId-tuple")
    split_event_id = stable_id("reflex:event:split")

    def child(payload: dict[str, Any]) -> dict[str, Any]:
        payload["parentContainer"] = {"targetUniqueIdentifier": container_id}
        return payload

    def attribute_ref(entity_id: str, name: str) -> dict[str, Any]:
        return {
            "kind": "AttributeReference",
            "type": "complex",
            "arguments": [{"name": "entityId", "type": "string", "value": entity_id}],
            "name": name,
        }

    def event_ref(entity_id: str) -> dict[str, Any]:
        return {
            "kind": "EventReference",
            "type": "complex",
            "arguments": [{"name": "entityId", "type": "string", "value": entity_id}],
            "name": "event",
        }

    # Combines the identity parts into the object's full key.
    tuple_instance = {
        "templateId": "IdentityTupleAttribute",
        "templateVersion": "1.1",
        "steps": [
            {
                "name": "IdStructureStep",
                "id": stable_id("reflex:step:identity-tuple"),
                "rows": [
                    {
                        "name": "IdPart",
                        "kind": "IdPart",
                        "arguments": [attribute_ref(identity_id, "idPart")],
                    }
                ],
            }
        ],
    }

    # Splits the raw stream into per-machine event streams. Attributes must read
    # from this view, not the raw source event, or the rule never evaluates.
    split_event_instance = {
        "templateId": "SplitEvent",
        "templateVersion": "1.1",
        "steps": [
            {
                "name": "SplitEventStep",
                "id": stable_id("reflex:step:split-event"),
                "rows": [
                    {
                        "name": "EventSelector",
                        "kind": "Event",
                        "arguments": [event_ref(event_id)],
                    },
                    {
                        "name": "FieldIdMapping",
                        "kind": "FieldIdMapping",
                        "arguments": [
                            {"name": "fieldName", "type": "string", "value": "machineId"},
                            attribute_ref(identity_id, "idPart"),
                        ],
                    },
                    {
                        "name": "SplitEventOptions",
                        "kind": "EventOptions",
                        "arguments": [
                            {"name": "isAuthoritative", "type": "boolean", "value": True}
                        ],
                    },
                ],
            }
        ],
    }

    identity_instance = {
        "templateId": "IdentityPartAttribute",
        "templateVersion": "1.1",
        "steps": [
            {
                "name": "IdPartStep",
                "id": stable_id("reflex:step:identity-part"),
                "rows": [
                    {
                        "name": "TypeAssertion",
                        "kind": "TypeAssertion",
                        "arguments": [
                            {"name": "op", "type": "string", "value": "Text"},
                            {"name": "format", "type": "string", "value": ""},
                        ],
                    }
                ],
            }
        ],
    }

    vibration_instance = {
        "templateId": "BasicEventAttribute",
        "templateVersion": "1.1",
        "steps": [
            {
                "name": "EventSelectStep",
                "id": stable_id("reflex:step:vibration-select"),
                "rows": [
                    {
                        "name": "EventSelector",
                        "kind": "Event",
                        "arguments": [
                            {
                                "kind": "EventReference",
                                "type": "complex",
                                "arguments": [
                                    {"name": "entityId", "type": "string",
                                     "value": split_event_id}
                                ],
                                "name": "event",
                            }
                        ],
                    },
                    {
                        "name": "EventFieldSelector",
                        "kind": "EventField",
                        "arguments": [
                            {"name": "fieldName", "type": "string", "value": "vibrationMm"}
                        ],
                    },
                ],
            },
            {
                "name": "EventComputeStep",
                "id": stable_id("reflex:step:vibration-compute"),
                "rows": [
                    {
                        "name": "TypeAssertion",
                        "kind": "TypeAssertion",
                        "arguments": [
                            {"name": "op", "type": "string", "value": "Number"},
                            {"name": "format", "type": "string", "value": ""},
                        ],
                    }
                ],
            },
        ],
    }

    rule_instance = {
        "templateId": "AttributeTrigger",
        "templateVersion": "1.1",
        "steps": [
            {
                "name": "ScalarSelectStep",
                "id": stable_id("reflex:step:rule-select"),
                "rows": [
                    {
                        "name": "AttributeSelector",
                        "kind": "Attribute",
                        "arguments": [
                            {
                                "kind": "AttributeReference",
                                "type": "complex",
                                "arguments": [
                                    {"name": "entityId", "type": "string", "value": vibration_id}
                                ],
                                "name": "attribute",
                            }
                        ],
                    },
                ],
            },
            {
                "name": "ScalarDetectStep",
                "id": stable_id("reflex:step:rule-detect"),
                "rows": [
                    {
                        "name": "NumberBecomes",
                        "kind": "NumberBecomes",
                        "arguments": [
                            {"name": "op", "type": "string", "value": "BecomesGreaterThan"},
                            {"name": "value", "type": "number", "value": 8.0},
                        ],
                    },
                    {"name": "OccurrenceOption", "kind": "EachTime", "arguments": []},
                ],
            },
            {
                "name": "ActStep",
                "id": stable_id("reflex:step:rule-act"),
                "rows": [
                    {
                        "name": "EmailBinding",
                        "kind": "EmailMessage",
                        "arguments": [
                            {"name": "messageLocale", "type": "string", "value": "en-us"},
                            {
                                "name": "sentTo",
                                "type": "array",
                                "values": [{"type": "string", "value": ALERT_RECIPIENT}],
                            },
                            {"name": "copyTo", "type": "array", "values": []},
                            {"name": "bCCTo", "type": "array", "values": []},
                            {
                                "name": "subject",
                                "type": "array",
                                "values": [
                                    {
                                        "type": "string",
                                        "value": "Contoso Motors - high vibration detected",
                                    }
                                ],
                            },
                            {
                                "name": "headline",
                                "type": "array",
                                "values": [
                                    {"type": "string", "value": "Machine vibration above 8.0 mm"}
                                ],
                            },
                            {
                                "name": "optionalMessage",
                                "type": "array",
                                "values": [
                                    {
                                        "type": "string",
                                        "value": (
                                            "Sustained vibration above the 8.0 mm safety "
                                            "threshold was detected. Inspect the machine and "
                                            "raise a maintenance request."
                                        ),
                                    }
                                ],
                            },
                            {"name": "additionalInformation", "type": "array", "values": []},
                        ],
                    },
                ],
            },
        ],
    }

    def encoded(instance: dict[str, Any]) -> str:
        return json.dumps(instance, separators=(",", ":"))

    entities: list[dict[str, Any]] = [
        {
            "uniqueIdentifier": container_id,
            "payload": {"name": "Contoso machine health", "type": "kqlQueries"},
            "type": "container-v1",
        },
    ]
    # NOTE: this tenant's Activator API rejects fabricItemAction-v1 entities
    # ("Invalid definition."), so the rule keeps the built-in email action and
    # the ACS notebook is driven by OA_FactoryOperations instead.
    _ = (action_notebook_id, workspace_id)
    if not live_source_id:
        entities.append(
            {
                "uniqueIdentifier": source_id,
                "payload": child(
                    {
                        "name": "Machine telemetry stream",
                        "metadata": {"eventstreamArtifactId": eventstream_id},
                    }
                ),
                "type": "eventstreamSource-v1",
            }
        )
    else:
        # Keep Fabric's own source entity in the definition; omitting it deletes
        # it and leaves the event bound to a dangling reference.
        entities.append(
            {
                "uniqueIdentifier": live_source_id,
                "payload": {
                    "name": "EventStream",
                    "metadata": {"eventstreamArtifactId": eventstream_id},
                },
                "type": "eventstreamSource-v1",
            }
        )
    entities += [
        {
            "uniqueIdentifier": event_id,
            "payload": child(
                {
                    "name": "Machine telemetry events",
                    "definition": {
                        "type": "Event",
                        "instance": encoded(
                            {
                                "templateId": "SourceEvent",
                                "templateVersion": "1.1",
                                "steps": [
                                    {
                                        "name": "SourceEventStep",
                                        "id": stable_id("reflex:step:source"),
                                        "rows": [
                                            {
                                                "name": "SourceSelector",
                                                "kind": "SourceReference",
                                                "arguments": [
                                                    {
                                                        "name": "entityId",
                                                        "type": "string",
                                                        "value": source_id,
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ),
                    },
                }
            ),
            "type": "timeSeriesView-v1",
        },
        {
            "uniqueIdentifier": object_id,
            "payload": child({"name": "Machine", "definition": {"type": "Object"}}),
            "type": "timeSeriesView-v1",
        },
        {
            "uniqueIdentifier": identity_id,
            "payload": child(
                {
                    "name": "machineId",
                    "parentObject": {"targetUniqueIdentifier": object_id},
                    "definition": {"type": "Attribute", "instance": encoded(identity_instance)},
                }
            ),
            "type": "timeSeriesView-v1",
        },
        {
            "uniqueIdentifier": tuple_id,
            "payload": child(
                {
                    "name": "machineId tuple",
                    "parentObject": {"targetUniqueIdentifier": object_id},
                    "definition": {"type": "Attribute", "instance": encoded(tuple_instance)},
                }
            ),
            "type": "timeSeriesView-v1",
        },
        {
            "uniqueIdentifier": split_event_id,
            "payload": child(
                {
                    "name": "Machine events",
                    "parentObject": {"targetUniqueIdentifier": object_id},
                    "definition": {
                        "type": "Event",
                        "instance": encoded(split_event_instance),
                    },
                }
            ),
            "type": "timeSeriesView-v1",
        },
        {
            "uniqueIdentifier": vibration_id,
            "payload": child(
                {
                    "name": "vibrationMm",
                    "parentObject": {"targetUniqueIdentifier": object_id},
                    "definition": {"type": "Attribute", "instance": encoded(vibration_instance)},
                }
            ),
            "type": "timeSeriesView-v1",
        },
        {
            "uniqueIdentifier": rule_id,
            "payload": child(
                {
                    "name": "High vibration",
                    "parentObject": {"targetUniqueIdentifier": object_id},
                    "definition": {
                        "type": "Rule",
                        "instance": encoded(rule_instance),
                        "settings": {"shouldRun": True, "shouldApplyRuleOnUpdate": True},
                    },
                }
            ),
            "type": "timeSeriesView-v1",
        },
    ]
    return entities


def find_live_reflex_source(
    client: FabricClient,
    workspace_id: str,
    reflex_id: str,
    eventstream_id: str,
    timeout: int = 180,
) -> str | None:
    """Return the eventstreamSource entity Fabric created for the destination.

    Fabric injects its own source entity when the Activator destination is added
    to the eventstream, and only that entity receives events. Ours is identified
    by a deterministic stable_id, so anything else is Fabric's. Injection lags
    the topology update, so poll rather than checking once.
    """
    ours = stable_id("reflex:stream-source")
    deadline = time.time() + timeout
    while True:
        deployed = current_definition(client, workspace_id, reflex_id)
        if deployed and "ReflexEntities.json" in deployed:
            entities = json.loads(
                base64.b64decode(deployed["ReflexEntities.json"]).decode("utf-8")
            )
            for entity in entities:
                if entity.get("type") != "eventstreamSource-v1":
                    continue
                if entity.get("uniqueIdentifier") == ours:
                    continue
                metadata = entity.get("payload", {}).get("metadata") or {}
                if metadata.get("eventstreamArtifactId") == eventstream_id:
                    return entity["uniqueIdentifier"]
        if time.time() >= deadline:
            return None
        time.sleep(10)


def ensure_reflex(
    client: FabricClient,
    workspace_id: str,
    eventstream_id: str,
    live_source_id: str | None = None,
    action_notebook_id: str | None = None,
) -> dict[str, Any]:
    description = (
        "High-vibration Activator: sustained vibration above 8.0 mm "
        "sends a maintenance alert email."
    )
    return ensure_generic_item(
        client,
        workspace_id,
        "ACT_MachineHealth",
        "Reflex",
        description,
        [
            inline_part(
                "ReflexEntities.json",
                reflex_entities(
                    eventstream_id, live_source_id, action_notebook_id, workspace_id
                ),
            ),
            inline_part(
                ".platform",
                platform("Reflex", "ACT_MachineHealth", description),
            ),
        ],
    )


def ensure_operations_agent(
    client: FabricClient,
    workspace_id: str,
    database_id: str,
    action_notebook_id: str,
) -> dict[str, Any]:
    instructions = """*** Operational Instructions ***
1. Monitor every machine for vibration above 8.0 mm.
2. Use a transition condition and recommend the SendMaintenanceEmail action when a machine crosses above 8.0 mm.
3. Do not recommend the action again until that machine returns to 8.0 mm or below and crosses above the threshold again.

*** Semantic Instructions ***
1. Machine telemetry is in the MachineTelemetry table.
2. Each machine is uniquely identified by machineId.
3. timestamp is the event time in UTC.
4. vibrationMm is vibration measured in millimeters.
5. line and plant identify the machine's production line and plant."""
    action_id = stable_id("operations-agent:send-maintenance-email")
    rule_id = stable_id("operations-agent:high-vibration-rule")

    def column_property(
        iri: str,
        name: str,
        description: str,
        range_type: str,
        kind: int,
        column_name: str,
    ) -> dict[str, Any]:
        return {
            "$type": "data",
            "IRI": iri,
            "DocumentId": "00000000-0000-0000-0000-000000000000",
            "Name": name,
            "Description": description,
            "DomainClassIRI": "Machine",
            "RangeDataType": range_type,
            "Kind": kind,
            "DataLink": {"$type": "kustotablecol", "ColumnName": column_name},
        }

    playbook = {
        "OntologyDefinitions": {
            "Machine": {
                "$type": "class",
                "IRI": "Machine",
                "DocumentId": "00000000-0000-0000-0000-000000000000",
                "Name": "Machine",
                "Description": "A factory machine represented by its latest telemetry record.",
                "DataLink": {"$type": "kustotable", "TableName": "MachineTelemetry"},
            },
            "machineId": column_property(
                "machineId", "machineId", "Unique machine identifier.", "string", 0, "machineId"
            ),
            "line": column_property(
                "line", "line", "Production line.", "string", 1, "line"
            ),
            "plant": column_property(
                "plant", "plant", "Manufacturing plant.", "string", 1, "plant"
            ),
            "timestamp": column_property(
                "timestamp", "timestamp", "Telemetry event time in UTC.", "datetime", 1, "timestamp"
            ),
            "vibrationMm": column_property(
                "vibrationMm", "vibrationMm", "Vibration in millimeters.", "decimal", 1,
                "vibrationMm",
            ),
        },
        "RuleDefinitions": {
            rule_id: {
                "Id": rule_id,
                "Name": "Recommend maintenance for high vibration",
                "Description": (
                    "Recommends a maintenance email when the latest machine vibration "
                    "is above 8.0 mm."
                ),
                "ClassExpression": {
                    "$type": "manchesterclassexp",
                    "Expression": (
                        "ClassExpression: AllMachines ``` Machine ``` "
                        'Annotations: metadata:description "All monitored factory machines"'
                    ),
                    "Description": "All monitored factory machines",
                },
                "RuleCondition": {
                    "$type": "propertywhenisabove",
                    "DataPropertyName": "vibrationMm",
                    "Threshold": 8.0,
                },
                "ActionBinding": {
                    "$type": "actionbinding",
                    "Name": "SendMaintenanceEmail",
                    "Description": "Send the predictive-maintenance email after approval.",
                    "ActionId": action_id,
                    "ParameterBindings": [
                        {
                            "$type": "parameterbindingcontextkey",
                            "Name": name,
                            "Key": f"agent:operationalSet:Machine:{name}",
                            "Description": description,
                        }
                        for name, description in (
                            ("machineId", "Machine identifier."),
                            ("line", "Production line."),
                            ("plant", "Manufacturing plant."),
                            ("vibrationMm", "Current vibration in millimeters."),
                            ("timestamp", "Telemetry event time."),
                        )
                    ],
                },
            }
        },
    }
    configuration = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/item/"
            "operationsAgents/definition/1.0.0/schema.json"
        ),
        "configuration": {
            "instructions": instructions,
            "dataSources": {
                database_id: {
                    "id": database_id,
                    "type": "KustoDatabase",
                    "workspaceId": workspace_id,
                }
            },
            "actions": {
                action_id: {
                    "id": action_id,
                    "displayName": "SendMaintenanceEmail",
                    "description": (
                        "Record a predictive-maintenance request for a machine that "
                        "crossed the high-vibration threshold."
                    ),
                    "kind": "FabricJobAction",
                    "connection": {
                        "jobArtifactId": action_notebook_id,
                        "jobWorkspaceId": workspace_id,
                        "itemType": "Notebook",
                        "jobType": "RunNotebook",
                        "subItemId": "",
                    },
                    "parameters": [
                        {"name": "machineId"},
                        {"name": "line"},
                        {"name": "plant"},
                        {"name": "vibrationMm"},
                        {"name": "timestamp"},
                    ],
                }
            },
            "messageDestination": {
                "kind": "Recipient",
                "recipient": agent_recipient(),
            },
        },
        "playbook": playbook,
        "shouldRun": True,
    }
    description = "Operations agent for machine-health monitoring and maintenance email action."
    existing = find_named(client.list_items(workspace_id), "OA_FactoryOperations", "OperationsAgent")
    if not existing:
        response = client.request(
            "POST",
            f"/workspaces/{workspace_id}/operationsAgents",
            {"displayName": "OA_FactoryOperations", "description": description},
        )
        result = client.complete(response)
        existing = result if result.get("id") else wait_for_named(
            client, workspace_id, "OA_FactoryOperations", "OperationsAgent"
        )
    else:
        deployed = current_definition(client, workspace_id, existing["id"])
        if deployed and "Configurations.json" in deployed:
            live = json.loads(
                base64.b64decode(deployed["Configurations.json"]).decode("utf-8")
            )
            live_actions = live.get("configuration", {}).get("actions") or {}
            kept = {
                key: value
                for key, value in live_actions.items()
                if value.get("kind") != "FabricJobAction"
                or (value.get("connection") or {}).get("jobWorkspaceId") == workspace_id
            }
            for key, value in live_actions.items():
                if key not in kept:
                    target = (value.get("connection") or {}).get("jobWorkspaceId")
                    print(
                        f"  Dropping Operations Agent action '{value.get('displayName')}' "
                        f"- it targets workspace {target}, outside this solution."
                    )
            if kept:
                configuration["configuration"]["actions"].update(kept)
            if live == configuration:
                # Fabric refuses updateDefinition while the agent is running,
                # even for an identical payload, so skip a no-op write.
                print("  Operations Agent already matches the desired definition.")
                return existing
    parts = [
            inline_part("Configurations.json", configuration),
            inline_part(
                ".platform",
                platform(
                    "OperationsAgent",
                    "OA_FactoryOperations",
                    "Machine-health monitoring and maintenance action agent.",
                ),
            ),
        ]
    client.complete(
        client.request(
            "POST",
            f"/workspaces/{workspace_id}/operationsAgents/{existing['id']}/updateDefinition",
            {"definition": {"format": "OperationsAgentV1", "parts": parts}},
        )
    )
    return existing


def eventhouse_cluster_uri(
    client: FabricClient,
    workspace_id: str,
    eventhouse_id: str,
) -> str:
    details = client.request(
        "GET",
        f"/workspaces/{workspace_id}/eventhouses/{eventhouse_id}",
        expected=(200,),
    ).json()
    cluster_uri = details.get("properties", {}).get("queryServiceUri")
    if not cluster_uri:
        raise DeploymentError("Eventhouse queryServiceUri isn't available.")
    return cluster_uri


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy the Contoso Motors RTI Fabric solution.")
    parser.add_argument(
        "--capacity-id",
        default=os.environ.get("FABRIC_CAPACITY_ID"),
        help="Fabric capacity GUID (the Fabric-side identifier, not the Azure resource ID). "
             "Required unless FABRIC_CAPACITY_ID is set.",
    )
    args = parser.parse_args()
    if not args.capacity_id:
        raise DeploymentError(
            "Set --capacity-id or FABRIC_CAPACITY_ID to your Fabric capacity's GUID. "
            "See README-RTI.md for how to find it."
        )
    ensure_capacity_active()
    client = FabricClient()

    workspace = ensure_workspace(client, args.capacity_id)
    workspace_id = workspace["id"]
    print(f"Workspace: {workspace_id}")

    eventhouse = ensure_eventhouse(client, workspace_id)
    print(f"Eventhouse: {eventhouse['id']}")
    database = ensure_kql_database(client, workspace_id, eventhouse["id"])
    print(f"KQL database: {database['id']}")
    cluster_uri = eventhouse_cluster_uri(client, workspace_id, eventhouse["id"])
    # Look the Activator up first so the eventstream keeps its Activator
    # destination on every run. Rewriting the topology without it deletes the
    # destination and silently disconnects the rule.
    known_activator = find_named(
        client.list_items(workspace_id), "ACT_MachineHealth", "Reflex"
    )
    eventstream = ensure_eventstream(
        client,
        workspace_id,
        database["id"],
        known_activator["id"] if known_activator else None,
    )
    print(f"Eventstream: {eventstream['id']}")
    maintenance_notebook = ensure_maintenance_notebook(client, workspace_id, cluster_uri)
    print(f"Maintenance action notebook: {maintenance_notebook['id']}")
    # Discover Fabric's injected stream source before rewriting the Activator,
    # otherwise the rewrite deletes it and the rule loses its live input.
    live_source = (
        find_live_reflex_source(
            client, workspace_id, known_activator["id"], eventstream["id"], timeout=0
        )
        if known_activator
        else None
    )
    activator = ensure_reflex(
        client, workspace_id, eventstream["id"], live_source,
        maintenance_notebook["id"],
    )
    print(f"Activator: {activator['id']}")
    if not known_activator:
        # First deployment: the Activator did not exist when the topology was
        # written, so add its destination now that it does.
        wait_for_eventstream_ready(client, workspace_id, eventstream["id"])
        eventstream = ensure_eventstream(
            client, workspace_id, database["id"], activator["id"]
        )
    wait_for_eventstream_ready(client, workspace_id, eventstream["id"])
    if not live_source:
        live_source = find_live_reflex_source(
            client, workspace_id, activator["id"], eventstream["id"], timeout=0
        )
    if not live_source:
        # Fabric only injects its stream-source entity when the Activator
        # destination is created, so recreate the destination to get one.
        print("  Recreating the Activator destination to obtain a live stream source...")
        ensure_eventstream(client, workspace_id, database["id"], None)
        wait_for_eventstream_ready(client, workspace_id, eventstream["id"])
        eventstream = ensure_eventstream(
            client, workspace_id, database["id"], activator["id"]
        )
        wait_for_eventstream_ready(client, workspace_id, eventstream["id"])
        live_source = find_live_reflex_source(
            client, workspace_id, activator["id"], eventstream["id"]
        )
    if live_source:
        activator = ensure_reflex(
            client, workspace_id, eventstream["id"], live_source,
            maintenance_notebook["id"],
        )
        print(f"Activator bound to live stream source {live_source}")
    else:
        print("WARNING: no live Activator stream source found; rule may not receive events.")

    artifacts = {
        "workspace": workspace,
        "eventhouse": eventhouse,
        "kqlDatabase": database,
        "eventstream": eventstream,
        "notebook": ensure_notebook(client, workspace_id),
        "maintenanceNotebook": maintenance_notebook,
        "queryset": ensure_queryset(client, workspace_id, cluster_uri, database["id"]),
        "activator": activator,
        "operationsAgent": ensure_operations_agent(
            client, workspace_id, database["id"], maintenance_notebook["id"]
        ),
        "dashboard": ensure_dashboard(client, workspace_id, cluster_uri, database["id"]),
    }
    artifacts["clusterUri"] = cluster_uri
    destination = state_path()
    destination.write_text(json.dumps(artifacts, indent=2), encoding="utf-8")
    print(f"Deployment state: {destination}")
    print(json.dumps({name: value.get("id") for name, value in artifacts.items() if isinstance(value, dict)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
