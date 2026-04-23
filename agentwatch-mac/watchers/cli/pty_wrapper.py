#!/usr/bin/env python3
"""
AgentWatch — pty_wrapper.py (Router)  v2.0

Routes to agent-specific wrappers:
  claude / claude-code  → watchers/cli/claude/wrapper.py
  gemini / gemini-cli   → watchers/cli/gemini/wrapper.py
  default               → watchers/cli/default/wrapper.py
"""

import sys
import os
import argparse

# Make sure the sibling packages are importable regardless of cwd
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def main():
    parser = argparse.ArgumentParser(description="AgentWatch PTY wrapper router", add_help=False)
    parser.add_argument("--agent", default="agent")
    args, _ = parser.parse_known_args()
    agent_name = args.agent.lower()

    if agent_name in ("claude", "claude-code", "claude-cli"):
        from claude.wrapper import main as wrapper_main
    elif agent_name in ("gemini", "gemini-cli"):
        from gemini.wrapper import main as wrapper_main
    else:
        from default.wrapper import main as wrapper_main

    wrapper_main()


if __name__ == "__main__":
    main()