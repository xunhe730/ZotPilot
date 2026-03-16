#!/usr/bin/env python3
"""ZotPilot skill script runner."""
import subprocess
import sys
import pathlib

script = pathlib.Path(__file__).parent / sys.argv[1]
sys.exit(subprocess.run([sys.executable, str(script)] + sys.argv[2:]).returncode)
