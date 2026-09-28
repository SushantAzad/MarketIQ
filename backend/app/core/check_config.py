"""Validate local settings without exposing secrets or implying service health."""

import json

from pydantic import ValidationError

from app.core.config import load_settings


def main() -> int:
    try:
        settings = load_settings()
    except ValidationError as exc:
        # Never print raw inputs, contexts, or exception strings containing secrets.
        print(
            json.dumps(
                {
                    "configuration": "invalid",
                    "errors": [
                        {"field": ".".join(map(str, error["loc"])), "type": error["type"]}
                        for error in exc.errors(include_input=False, include_context=False)
                    ],
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "configuration": "valid",
                "environment": settings.app_env,
                "sec_enabled": settings.sec_enabled,
                "market_provider": settings.market_data_provider,
                "connectivity_tested": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
