"""One JSON record per event; database credentials are never logged."""
import json
from datetime import datetime, timezone


def log_event(event: str, **fields) -> None:
    print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                      "event": event, **fields}, ensure_ascii=False, default=str), flush=True)
