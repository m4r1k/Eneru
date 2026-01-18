#!/usr/bin/env python3
"""
Wrapper script for Eneru deb/rpm package installation.
This script is installed as /opt/ups-monitor/eneru.py and invokes the CLI.
"""
import sys

# Add the package directory to Python path so 'eneru' module can be found
sys.path.insert(0, '/opt/ups-monitor')

from eneru.cli import main

if __name__ == "__main__":
    main()
