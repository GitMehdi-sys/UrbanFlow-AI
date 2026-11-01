#!/usr/bin/env python3
"""
Ditwin Web Interface

Web interface launcher script.
"""

import os
import sys
from pathlib import Path

# Load API key from file
api_key_file = Path(__file__).parent / "claude_api.txt"
if api_key_file.exists():
    with open(api_key_file) as f:
        api_key = f.read().strip()
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key


def main():
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description='Ditwin Web Interface')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8000, help='Port to bind (default: 8000)')
    parser.add_argument('--reload', action='store_true', help='Enable auto-reload for development')
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  Ditwin Web Interface")
    print("=" * 60)
    print(f"\n  Starting server at http://{args.host}:{args.port}")
    print(f"  Open in browser: http://localhost:{args.port}")
    print("\n  Press Ctrl+C to stop\n")
    print("=" * 60 + "\n")

    uvicorn.run(
        "web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info"
    )


if __name__ == "__main__":
    main()
