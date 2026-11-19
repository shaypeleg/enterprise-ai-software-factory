"""Run from a target checkout or pass --project to install the Codex skill."""

import sys

from factory.__main__ import main


if __name__ == "__main__":
    # CLI options for project selection precede the install subcommand.
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="target Git checkout (default: current directory)")
    parser.add_argument("--no-browser", action="store_true", help="print the private setup URL")
    parser.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.argv = [sys.argv[0]] + (["--project", args.project] if args.project else []) + ["install"]
    if args.managed:
        sys.argv.append("--managed")
    if args.no_browser:
        sys.argv.append("--no-browser")
    main()
