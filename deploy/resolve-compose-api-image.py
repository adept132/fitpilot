#!/usr/bin/env python3
"""Print only the API image reference from Docker Compose config JSON.

Compose's ``config --images`` lists every service even when ``api`` is passed.
For a build-only API service, Compose names its image ``<project>-api``.
"""

import json
import re
import sys


IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*\Z")
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


def main() -> int:
    try:
        config = json.load(sys.stdin)
        service = config["services"]["api"]
        image = service.get("image")
        if image is None:
            project = config["name"]
            if not isinstance(project, str) or not PROJECT.fullmatch(project) or "build" not in service:
                raise ValueError
            image = f"{project}-api"
        if not isinstance(image, str) or not IMAGE_REF.fullmatch(image):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        print("error=compose_api_image_ref_invalid", file=sys.stderr)
        return 1
    print(image)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
