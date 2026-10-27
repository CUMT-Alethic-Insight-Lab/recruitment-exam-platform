import argparse
import math

from .server import CliRunner, serve


def main():
    parser = argparse.ArgumentParser(description="Alethic local stdio MCP adapter")
    parser.add_argument("--cli", default="alethic", help="Trusted local CLI executable path (not a shell command)")
    parser.add_argument("--timeout", type=float, default=15.0, help="Per CLI invocation timeout, 0.05–120 seconds")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0.05 <= args.timeout <= 120:
        parser.error("--timeout must be between 0.05 and 120 seconds")
    serve(CliRunner((args.cli,), args.timeout))


if __name__ == "__main__":
    main()
