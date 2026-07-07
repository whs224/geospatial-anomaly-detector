"""Process-lifecycle helpers shared by the long-running loop services."""

import signal
import time
from pathlib import Path

# Touched every loop iteration; the container healthcheck asserts freshness.
HEARTBEAT_PATH = Path('/tmp/heartbeat')

_HEARTBEAT_TICK_SECONDS = 15.0


def heartbeat() -> None:
    try:
        HEARTBEAT_PATH.touch()
    except OSError:
        pass  # never let liveness reporting break the pipeline


def sleep_with_heartbeat(seconds: float) -> None:
    """Sleep in short ticks so the heartbeat stays fresh during long backoffs
    and SIGTERM is honored promptly."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        heartbeat()
        time.sleep(min(_HEARTBEAT_TICK_SECONDS, remaining))


def _raise_system_exit(signum, frame) -> None:
    raise SystemExit(0)


def install_sigterm_handler() -> None:
    """Make `docker stop` terminate the loop cleanly instead of via SIGKILL."""
    signal.signal(signal.SIGTERM, _raise_system_exit)
