#!/usr/bin/env python3
"""
AgentWatch — pty_wrapper.py (Router)
Routes the PTY wrapping to either the gemini-specific wrapper or the default wrapper.
"""

import sys
import argparse
import importlib
import os

def main():
    parser = argparse.ArgumentParser(description="AgentWatch PTY wrapper router", add_help=False)
    # We only parse enough to know which agent we are dealing with.
    # We use parse_known_args because we just want to extract --agent.
    parser.add_argument("--agent", default="agent")
    
    args, _ = parser.parse_known_args()
    
    agent_name = args.agent
    
    if agent_name == "gemini" or agent_name == "gemini-cli":
        from gemini.wrapper import main as wrapper_main
    else:
        from default.wrapper import main as wrapper_main
        
    wrapper_main()

if __name__ == "__main__":
    main()
