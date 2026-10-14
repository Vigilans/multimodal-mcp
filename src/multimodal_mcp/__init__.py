import argparse

from .config import load
from .server import build


def main() -> None:
    parser = argparse.ArgumentParser(prog="multimodal-mcp")
    parser.add_argument("--config", help="Config file (default: ~/.agents/mcp/multimodal/config.toml)")
    args = parser.parse_args()
    build(load(args.config)).run(show_banner=False)
