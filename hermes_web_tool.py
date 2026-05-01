#!/usr/bin/env python3
"""Hermes web tool — Lightpanda-powered web fetch and dump.

Usage:
    web lightpanda markdown <url> [--wait-ms=5000] [--strip=js,css]
    web lightpanda html <url>
    web lightpanda mcp     # start MCP server on port 9222

Lightpanda is a headless browser written in Zig:
    - 16x less RAM than Chrome (~123MB vs 2GB for 100 pages)
    - 9x faster execution (5s vs 46s for 100 pages)
    - Built-in JavaScript execution (V8)
    - Supports Markdown, HTML, and semantic tree dumps
"""

import subprocess
import sys
import argparse


def lightpanda_fetch(url: str, dump_format: str = "markdown", wait_ms: int = 5000, strip: str = "") -> str:
    """Fetch a URL using Lightpanda and return the dumped content."""
    cmd = ["docker", "run", "--rm", "--entrypoint", "/usr/bin/lightpanda", "lightpanda/browser",
           "fetch", f"--dump", dump_format, f"--wait-ms", str(wait_ms)]
    if strip:
        cmd += ["--strip-mode", strip]
    cmd.append(url)
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"Lightpanda failed: {result.stderr}")
    return result.stdout


def lightpanda_mcp(port: int = 9222):
    """Start Lightpanda MCP server."""
    import os
    os.execvp("docker", ["docker", "run", "--rm", "-p", f"{port}:{port}",
                          "--entrypoint", "/usr/bin/lightpanda", "lightpanda/browser",
                          "mcp", "--port", str(port)])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lightpanda web fetch tool")
    subparsers = parser.add_subparsers(dest="command")
    
    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("url")
    fetch_parser.add_argument("--dump", default="markdown", choices=["html", "markdown", "semantic_tree", "semantic_tree_text"])
    fetch_parser.add_argument("--wait-ms", type=int, default=5000)
    fetch_parser.add_argument("--strip", default="")
    
    mcp_parser = subparsers.add_parser("mcp")
    mcp_parser.add_argument("--port", type=int, default=9222)
    
    args = parser.parse_args()
    
    if args.command == "fetch":
        result = lightpanda_fetch(args.url, args.dump, args.wait_ms, args.strip)
        print(result)
    elif args.command == "mcp":
        lightpanda_mcp(args.port)
    else:
        parser.print_help()
