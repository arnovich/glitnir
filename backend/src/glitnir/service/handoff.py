"""Run an installed notebook handoff through bounded JSON stdin/stdout."""

import contextlib
import json
import sys

from glitnir.providers.handoff import load_handoffs


def main() -> None:
    """Dispatch only explicit list, prepare or submit requests from the editor."""
    try:
        request = json.loads(sys.stdin.read(200_001))
        with contextlib.redirect_stdout(sys.stderr):
            providers = load_handoffs()
            action = request["action"]
            if action == "list":
                result = [
                    {"name": p.name, "label": p.label} for p in providers.values()
                ]
            else:
                provider = providers[request["provider"]]
                if action not in {"prepare", "submit"}:
                    raise ValueError("Unsupported handoff action")
                result = getattr(provider, action)(request["payload"], request["token"])
        print(json.dumps(result))
    except Exception:
        # Provider exceptions may contain credentials or server response bodies.
        print(
            json.dumps(
                {
                    "error": (
                        "Handoff failed; check credentials, destination and saved "
                        "request. No automatic retry was made."
                    )
                }
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
