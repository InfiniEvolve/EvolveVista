"""evolve-vista CAMPAIGN: serve the campaign page for one campaign directory, running or finished, until Ctrl-C."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .server import DEFAULT_PORT, start
from .vista import Analysts

ANALYST_HELP = ("an agent the reader may pick to analyse a session: <agent>[/<model>[:<effort>]] (claude, codex, "
                "gemini, qwen, opencode, cursor, copilot, amp, goose, droid) or <name>=<shell command>; repeatable. "
                "Installed agents are offered without it")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The options of the page, shared with `infinievolve watch` and `infinievolve run --watch`."""
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"the page's port (default {DEFAULT_PORT})")
    parser.add_argument("--open", action="store_true", help="open the page in the browser")
    parser.add_argument("--analyst", action="append", default=[], metavar="AGENT", help=ANALYST_HELP)
    parser.add_argument("--no-agents", action="store_true", help="start no agents; the page asks a model directly")


def serve(root: Path, args, reader=None):
    """Start the page in a background thread from parsed options; print its address."""
    try:
        analysts = None if args.no_agents else Analysts(args.analyst)
    except ValueError as error:
        raise SystemExit(f"--analyst: {error}")
    server = start(root, args.port, analysts=analysts, reader=reader)
    print(f"watch the campaign at {server.url}", flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(server.url)
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="evolve-vista", description=__doc__)
    parser.add_argument("campaign", type=Path, help="an InfiniEvolve campaign directory")
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        server = serve(args.campaign, args)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    try:
        server.closing.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
