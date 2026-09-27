"""Run sync.sh daily while keeping the container alive between runs."""

import os
import signal
import subprocess
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


SYNC_SCRIPT = Path(__file__).with_name("sync.sh")


def next_run(now: datetime, at: time) -> datetime:
    """Next local wall-clock run, strictly after now (never replay missed runs)."""
    day: date = now.date()
    due = datetime.combine(day, at, tzinfo=now.tzinfo)
    if due <= now:
        due = datetime.combine(day + timedelta(days=1), at, tzinfo=now.tzinfo)
    return due


def main() -> None:
    hour, minute = map(int, os.environ.get("SYNC_TIME", "00:00").split(":"))
    at = time(hour, minute)
    zone = ZoneInfo(os.environ.get("TZ", "UTC"))
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())

    while not stopping.is_set():
        due = next_run(datetime.now(zone), at)
        print(f"Next sync: {due.isoformat()}", flush=True)
        while not stopping.is_set():
            # Compare UTC instants: subtraction of two datetimes with the same
            # ZoneInfo ignores the DST offset change between local midnights.
            seconds = (due.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
            if seconds <= 0:
                break
            stopping.wait(min(seconds, 60))
        if stopping.is_set():
            break

        print(f"Starting sync: {datetime.now(zone).isoformat()}", flush=True)
        child = subprocess.Popen(["bash", str(SYNC_SCRIPT)], start_new_session=True)
        while child.poll() is None:
            if stopping.wait(0.25):
                # sync.sh can be running a Python child; stop the whole group.
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass  # The child finished between poll() and SIGTERM.
                child.wait()
                break
        if stopping.is_set():
            break
        print(f"Sync finished with exit code {child.returncode}", flush=True)
    print("Scheduler stopped", flush=True)


if __name__ == "__main__":
    main()
