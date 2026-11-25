"""Command line for the local lab."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import URLError

from .fixtures import SCENARIOS
from .grade import grade, set_scenario
from .http import decode, request
from .identity import PERSONAS, mint_token
from .smoke import smoke
from .source import SourceServer

DEFAULT_SOURCE = "http://127.0.0.1:8765"
DEFAULT_API = "http://127.0.0.1:8000"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start the read-only source emulator")
    serve.add_argument("--port", type=int, default=8765)

    status = commands.add_parser("status", help="show the active source scenario")
    scenario = commands.add_parser("scenario", help="switch scenarios on the running source")
    scenario.add_argument("name", choices=SCENARIOS)
    for command in (status, scenario):
        command.add_argument("--source", default=os.getenv("HARBORLINE_SOURCE_URL", DEFAULT_SOURCE))

    token = commands.add_parser("token", help="issue a local practice JWT")
    token.add_argument("persona", choices=PERSONAS)
    token.add_argument("--ttl", type=int, default=3600)

    commands.add_parser("smoke", help="check the supplied lab and fixtures")
    acceptance = commands.add_parser("grade", help="run first-handoff acceptance against your application")
    acceptance.add_argument("--api", default=DEFAULT_API)
    acceptance.add_argument("--index", type=Path, default=Path(os.getenv("HARBORLINE_INDEX", ".local/index.sqlite3")))

    args = parser.parse_args()
    try:
        if args.command == "serve":
            with SourceServer(args.port) as server:
                print(f"Harborline source running at http://127.0.0.1:{server.server_port} (baseline)", flush=True)
                server.serve_forever()
        elif args.command == "status":
            status_code, _, body = request(args.source.rstrip("/") + "/health")
            if status_code != 200:
                raise RuntimeError(f"source returned HTTP {status_code}")
            print(json.dumps(decode(body)))
        elif args.command == "scenario":
            set_scenario(args.source.rstrip("/"), args.name)
            print(f"Source scenario is now {args.name}. Run your sync to bring it into your index.")
        elif args.command == "token":
            print(mint_token(args.persona, args.ttl))
        elif args.command == "smoke":
            return smoke()
        elif args.command == "grade":
            return grade(args.api, args.index)
    except KeyboardInterrupt:
        return 0
    except (OSError, URLError, RuntimeError) as error:
        print(f"Harborline lab: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
