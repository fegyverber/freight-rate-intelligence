"""Create a random password once; no checked-in or default database password."""
import os
import secrets
from pathlib import Path

target = Path("/secrets/postgres_password")
if not target.exists():
    value = os.environ.get("POSTGRES_PASSWORD") or secrets.token_urlsafe(36)
    # The file is only mounted into the database and pipeline containers.
    # Readability is needed by the postgres image's unprivileged server user.
    with target.open("x") as handle:
        handle.write(value + "\n")
    target.chmod(0o444)
print("Database credentials ready", flush=True)
