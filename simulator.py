from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from azure.eventhub import EventData, EventHubProducerClient, TransportType
from azure.eventhub.exceptions import AuthenticationError, ConnectError


MACHINES = tuple(f"M-{number:03d}" for number in range(1, 11))
LINES = {
    "M-001": "Line-A", "M-002": "Line-A", "M-003": "Line-A", "M-004": "Line-B",
    "M-005": "Line-B", "M-006": "Line-B", "M-007": "Line-C", "M-008": "Line-C",
    "M-009": "Line-C", "M-010": "Line-A",
}
PLANTS = {
    machine: ("Plant-Chennai" if index < 5 else "Plant-Pune")
    for index, machine in enumerate(MACHINES)
}


def connection_string_values(connection_string: str) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for segment in connection_string.split(";")
        if "=" in segment
        for key, value in [segment.split("=", 1)]
    }


def validate_connection_settings(
    connection_string: str,
    eventhub_name: str | None,
) -> str:
    required_parts = ("Endpoint=sb://", "SharedAccessKeyName=", "SharedAccessKey=")
    if not all(part in connection_string for part in required_parts):
        raise ValueError(
            "EVENTSTREAM_CONN_STR must be the complete Event Hub-compatible connection "
            "string copied from the Eventstream Custom Endpoint. A namespace hostname "
            "alone isn't sufficient. Expected format: "
            "'Endpoint=sb://<namespace>.servicebus.windows.net/;"
            "SharedAccessKeyName=<name>;SharedAccessKey=<key>;EntityPath=<event-hub>'."
        )
    values = connection_string_values(connection_string)
    entity_path = values.get("EntityPath")
    supplied_name = eventhub_name.strip() if eventhub_name else ""
    if entity_path and supplied_name and entity_path != supplied_name:
        raise ValueError(
            f"EVENTSTREAM_EH_NAME '{supplied_name}' doesn't match the connection "
            f"string EntityPath '{entity_path}'. Copy both values from the same "
            "Event Hub protocol details pane, or unset EVENTSTREAM_EH_NAME."
        )
    resolved_name = entity_path or supplied_name
    if not resolved_name:
        raise ValueError(
            "The connection string has no EntityPath, so EVENTSTREAM_EH_NAME is required."
        )
    return resolved_name


@dataclass(frozen=True)
class SimulatorConfig:
    duration_seconds: int = 900
    interval_seconds: float = 2.0
    anomaly_machine: str | None = None
    anomaly_window_seconds: int = 60
    anomaly_start_seconds: int = 0

    def validate(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        if self.anomaly_window_seconds < 0:
            raise ValueError("anomaly_window_seconds can't be negative")
        if self.anomaly_start_seconds < 0:
            raise ValueError("anomaly_start_seconds can't be negative")
        if self.anomaly_machine is not None and self.anomaly_machine not in MACHINES:
            raise ValueError(f"anomaly_machine must be one of {', '.join(MACHINES)}")


def make_event(machine_id: str, anomaly: bool = False) -> dict[str, object]:
    if machine_id not in MACHINES:
        raise ValueError(f"Unknown machine: {machine_id}")

    if anomaly:
        temperature = round(random.uniform(85.0, 100.0), 1)
        vibration = round(random.uniform(8.1, 14.0), 1)
        status = "fault"
    else:
        temperature = round(random.uniform(60.0, 75.0), 1)
        vibration = round(random.uniform(2.0, 5.0), 1)
        status = random.choice(("running", "running", "running", "idle"))

    return {
        "machineId": machine_id,
        "line": LINES[machine_id],
        "plant": PLANTS[machine_id],
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "temperatureC": temperature,
        "vibrationMm": vibration,
        "rpm": random.randint(1000, 1300),
        "throughputUnits": random.randint(30, 55),
        "status": status,
    }


def build_events(elapsed_seconds: float, config: SimulatorConfig) -> Iterable[dict[str, object]]:
    # The Activator rule uses a "becomes greater than" transition, so the machine
    # must report healthy values before the spike or there is nothing to detect.
    start = config.anomaly_start_seconds
    anomaly_active = start <= elapsed_seconds < start + config.anomaly_window_seconds
    return (
        make_event(
            machine_id,
            anomaly=machine_id == config.anomaly_machine and anomaly_active,
        )
        for machine_id in MACHINES
    )


class StaleCredentialsError(RuntimeError):
    """The supplied connection string no longer matches the live endpoint."""


def run(
    config: SimulatorConfig,
    connection_string: str,
    eventhub_name: str | None,
    should_stop: Callable[[], bool] = lambda: False,
    transport: TransportType | None = None,
) -> int:
    config.validate()
    resolved_eventhub_name = validate_connection_settings(connection_string, eventhub_name)
    sent = 0
    start = time.monotonic()
    producer = EventHubProducerClient.from_connection_string(
        connection_string,
        eventhub_name=None if connection_string_values(connection_string).get("EntityPath") else resolved_eventhub_name,
        transport_type=transport or TransportType.Amqp,
    )

    try:
        with producer:
            while time.monotonic() - start < config.duration_seconds and not should_stop():
                elapsed = time.monotonic() - start
                batch = producer.create_batch()
                for event in build_events(elapsed, config):
                    batch.add(EventData(json.dumps(event, separators=(",", ":"))))
                    sent += 1
                producer.send_batch(batch)

                remaining = config.interval_seconds
                while remaining > 0 and not should_stop():
                    sleep_for = min(0.2, remaining)
                    time.sleep(sleep_for)
                    remaining -= sleep_for
    except AuthenticationError as error:
        raise StaleCredentialsError(
            "Eventstream SAS authentication failed - the connection string is not "
            "valid for the current Custom Endpoint."
        ) from error
    return sent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send Contoso machine telemetry to Fabric Eventstream.")
    parser.add_argument("--duration", type=int, default=900, help="Run duration in seconds.")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between batches.")
    parser.add_argument("--anomaly-machine", default=None, choices=MACHINES)
    parser.add_argument("--anomaly-window", type=int, default=60, help="Anomaly duration in seconds.")
    parser.add_argument(
        "--anomaly-start",
        type=int,
        default=90,
        help="Seconds of healthy baseline before the anomaly begins.",
    )
    return parser.parse_args()


def fetch_connection_string() -> str | None:
    """Read the Custom Endpoint connection string from the Fabric REST API.

    Fabric exposes this via the eventstream source connection endpoint, so the
    demo does not need the credentials copied by hand. Returns None if the
    deployment state or Azure CLI login is unavailable.
    """
    try:
        import requests
    except ImportError:
        return None
    state_path = Path(__file__).resolve().parent / "deployment-state.json"
    override = os.environ.get("RTI_STATE_FILE")
    if override:
        candidate = Path(override)
        state_path = (
            candidate if candidate.is_absolute()
            else Path(__file__).resolve().parent / candidate
        )
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        access = subprocess.run(
            ["az", "account", "get-access-token", "--resource",
             "https://api.fabric.microsoft.com", "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, shell=True, check=False,
        )
        if access.returncode != 0 or not access.stdout.strip():
            return None
        headers = {"Authorization": "Bearer " + access.stdout.strip()}
        workspace_id = state["workspace"]["id"]
        eventstream_id = state["eventstream"]["id"]
        base = (
            f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
            f"/eventstreams/{eventstream_id}"
        )
        topology = requests.get(f"{base}/topology", headers=headers, timeout=90)
        topology.raise_for_status()
        source = next(
            item for item in topology.json()["sources"] if item["type"] == "CustomEndpoint"
        )
        connection = requests.get(
            f"{base}/sources/{source['id']}/connection", headers=headers, timeout=90
        )
        connection.raise_for_status()
        return connection.json()["accessKeys"]["primaryConnectionString"]
    except Exception:
        return None


def main() -> int:
    args = parse_args()
    connection_string = os.environ.get("EVENTSTREAM_CONN_STR")
    eventhub_name = os.environ.get("EVENTSTREAM_EH_NAME")
    from_environment = bool(connection_string)
    if not connection_string:
        connection_string = fetch_connection_string()
        if connection_string:
            eventhub_name = None
            print("Using the current Custom Endpoint credentials from Fabric.")
    if not connection_string:
        raise RuntimeError(
            "Could not read the Eventstream credentials from Fabric. Run 'az login', "
            "or set EVENTSTREAM_CONN_STR to the complete Event Hub connection string."
        )

    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    config = SimulatorConfig(
        duration_seconds=args.duration,
        interval_seconds=args.interval,
        anomaly_machine=args.anomaly_machine,
        anomaly_window_seconds=args.anomaly_window,
        anomaly_start_seconds=args.anomaly_start,
    )
    transport = (
        TransportType.AmqpOverWebsocket
        if os.environ.get("EVENTSTREAM_TRANSPORT", "").lower() in ("websocket", "websockets", "443")
        else TransportType.Amqp
    )
    try:
        sent = run(config, connection_string, eventhub_name, lambda: stopping, transport)
    except ConnectError:
        # Many corporate networks block AMQP on 5671. Event Hubs supports the
        # same protocol tunnelled over 443, so retry there before giving up.
        if transport is TransportType.AmqpOverWebsocket:
            raise
        print("AMQP port 5671 is unreachable; retrying over WebSockets on port 443...")
        transport = TransportType.AmqpOverWebsocket
        sent = run(config, connection_string, eventhub_name, lambda: stopping, transport)
        print("Connected over WebSockets. Set EVENTSTREAM_TRANSPORT=websocket to skip this retry.")
    except StaleCredentialsError:
        # Updating the eventstream topology replaces the Custom Endpoint, which
        # invalidates any connection string saved in the shell. Recover instead
        # of failing, since the live credentials are readable from Fabric.
        if not from_environment:
            raise
        print("EVENTSTREAM_CONN_STR is stale; falling back to the live credentials...")
        refreshed = fetch_connection_string()
        if not refreshed:
            raise RuntimeError(
                "EVENTSTREAM_CONN_STR is no longer valid and the live credentials "
                "could not be read. Run 'az login', then clear the variable with "
                "Remove-Item Env:\\EVENTSTREAM_CONN_STR and retry."
            ) from None
        sent = run(config, refreshed, None, lambda: stopping, transport)
        print("Recovered using the current Custom Endpoint credentials.")
    print(f"Sent {sent} telemetry events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
