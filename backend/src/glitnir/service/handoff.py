"""Run an installed notebook handoff through bounded JSON stdin/stdout."""

import contextlib
import json
import sys

from glitnir.providers.handoff import load_handoffs


def main() -> None:
    """Dispatch explicit discovery, preparation, submission and status requests."""
    try:
        request = json.loads(sys.stdin.read(200_001))
        with contextlib.redirect_stdout(sys.stderr):
            providers = load_handoffs()
            action = request["action"]
            if action == "list":
                result = [
                    {
                        "name": p.name,
                        "label": p.label,
                        "supportsStatus": p.status is not None,
                    }
                    for p in providers.values()
                ]
            else:
                provider = providers[request["provider"]]
                if action not in {"prepare", "submit", "status"}:
                    raise ValueError("Unsupported handoff action")
                callback = getattr(provider, action)
                if callback is None:
                    raise ValueError("This handoff does not support status refresh")
                result = callback(request["payload"], request["token"])
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
