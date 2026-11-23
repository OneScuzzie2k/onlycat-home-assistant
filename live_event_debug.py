"""Live OnlyCat event debugger.

Usage:
  $env:ONLYCAT_API_KEY="your_api_key_here"
  python ./live_event_debug.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Avoid local module shadowing (this repo has select.py, which breaks stdlib imports).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _SCRIPT_DIR]

import asyncio
import aiohttp
import socketio

ONLYCAT_URL = "https://gateway.onlycat.com"

TRIGGER_SOURCE = {
    0: "MANUAL",
    1: "REMOTE",
    2: "INDOOR_MOTION",
    3: "OUTDOOR_MOTION",
}

CLASSIFICATION = {
    1: "CLEAR",
    2: "SUSPICIOUS",
    3: "CONTRABAND",
    4: "HUMAN_ACTIVITY",
    10: "REMOTE_UNLOCK",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def body_of(payload: dict[str, Any]) -> dict[str, Any]:
    body = payload.get("body")
    return body if isinstance(body, dict) else payload


def brief(payload: dict[str, Any]) -> str:
    b = body_of(payload)
    src = b.get("eventTriggerSource")
    cls = b.get("eventClassification")
    return (
        f"device={payload.get('deviceId', b.get('deviceId'))} "
        f"event={payload.get('eventId', b.get('eventId'))} "
        f"ts={b.get('timestamp')} "
        f"src={src}({TRIGGER_SOURCE.get(src, 'UNKNOWN')}) "
        f"class={cls}({CLASSIFICATION.get(cls, 'UNKNOWN')}) "
        f"rfid={b.get('rfidCode') or b.get('rfidCodes')} "
        f"flap={b.get('flapState')} motion={b.get('motionSensorState')} "
        f"frameCount={b.get('frameCount')}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--api-key",
        "--token",
        dest="api_key",
        default=os.environ.get("ONLYCAT_API_KEY") or os.environ.get("ONLYCAT_TOKEN"),
        help="OnlyCat API key (--token and ONLYCAT_TOKEN remain compatibility aliases)",
    )
    parser.add_argument("--device-id", default=None)
    parser.add_argument("--raw", action="store_true", help="Print full JSON payloads")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=15,
        help="Polling interval for getDeviceEvents fallback (0 disables polling).",
    )
    args = parser.parse_args()

    if not args.api_key:
        raise SystemExit("Set ONLYCAT_API_KEY or pass --api-key.")

    async with aiohttp.ClientSession() as session:
        sio = socketio.AsyncClient(
            http_session=session,
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=5,
            reconnection_delay_max=10,
            ssl_verify=True,
        )
        known_event_ids: dict[str, int] = {}
        subscribed_device_ids: list[str] = []

        async def maybe_print(event_name: str, payload: dict[str, Any]) -> None:
            device_id = payload.get("deviceId") or body_of(payload).get("deviceId")
            if args.device_id and device_id != args.device_id:
                return
            print(f"[{now_utc()}] {event_name}: {brief(payload)}")
            if args.raw:
                print(json.dumps(payload, indent=2, default=str))

        @sio.on("connect")
        async def on_connect() -> None:
            print(f"[{now_utc()}] connected")
            devices = await sio.call("getDevices", {"subscribe": True})
            print(f"[{now_utc()}] getDevices -> {devices}")
            for d in devices or []:
                did = d.get("deviceId")
                if args.device_id and did != args.device_id:
                    continue
                await sio.call("getDevice", {"deviceId": did, "subscribe": True})
                initial_events = await sio.call(
                    "getDeviceEvents", {"deviceId": did, "subscribe": True}
                )
                print(f"[{now_utc()}] subscribed device={did}")
                subscribed_device_ids.append(did)
                if isinstance(initial_events, list) and initial_events:
                    initial_events.sort(
                        key=lambda e: (e or {}).get("eventId") or 0, reverse=True
                    )
                    newest = initial_events[0]
                    known_event_ids[did] = newest.get("eventId") or 0
                    await maybe_print("getDeviceEvents(initial)", newest)
                else:
                    known_event_ids[did] = 0

        @sio.on("disconnect")
        async def on_disconnect() -> None:
            print(f"[{now_utc()}] disconnected")

        @sio.on("deviceEventUpdate")
        async def on_device_event_update(payload: dict[str, Any]) -> None:
            await maybe_print("deviceEventUpdate", payload)
            did = payload.get("deviceId")
            eid = payload.get("eventId")
            if did and eid:
                try:
                    event = await sio.call(
                        "getEvent",
                        {"deviceId": did, "eventId": eid, "subscribe": True},
                    )
                    if isinstance(event, dict):
                        await maybe_print("getEvent", event)
                except Exception as exc:
                    print(f"[{now_utc()}] getEvent failed: {exc}")

        @sio.on("eventUpdate")
        async def on_event_update(payload: dict[str, Any]) -> None:
            await maybe_print("eventUpdate", payload)

        @sio.on("*")
        async def on_any(event_name: str, payload: Any) -> None:
            if event_name in {"deviceEventUpdate", "eventUpdate"}:
                return
            if isinstance(payload, dict):
                await maybe_print(event_name, payload)
            else:
                print(f"[{now_utc()}] {event_name}: {payload}")

        async def poll_events_loop() -> None:
            if args.poll_seconds <= 0:
                return
            while True:
                await asyncio.sleep(args.poll_seconds)
                if not sio.connected:
                    continue
                for did in subscribed_device_ids:
                    try:
                        events = await sio.call("getDeviceEvents", {"deviceId": did})
                    except Exception as exc:
                        print(f"[{now_utc()}] poll getDeviceEvents failed: {exc}")
                        continue
                    if not isinstance(events, list) or not events:
                        continue
                    events.sort(
                        key=lambda e: (e or {}).get("eventId") or 0, reverse=True
                    )
                    newest = events[0]
                    newest_id = newest.get("eventId") or 0
                    if newest_id > known_event_ids.get(did, 0):
                        known_event_ids[did] = newest_id
                        await maybe_print("getDeviceEvents(poll-newest)", newest)

        await sio.connect(
            ONLYCAT_URL,
            transports=["websocket"],
            namespaces="/",
            headers={"platform": "home-assistant", "device": "onlycat-live-debug"},
            auth={"token": args.api_key},
        )
        asyncio.create_task(poll_events_loop())
        await sio.wait()


if __name__ == "__main__":
    asyncio.run(main())
