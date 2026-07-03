#!/usr/bin/env python3
"""
Dynamic Ansible inventory — reads grid5000/.node_info.json.

Returns a single 'baremetal' group containing the reserved Grid5000 node.

SSH routing (single ProxyJump, built automatically from ~/.python-grid5000.yaml):
  laptop → access.grid5000.fr → node

Run as:  ansible-inventory -i ansible/inventory/dynamic.py --list
"""
import json
import os
import socket
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

NODE_FILE = Path(__file__).resolve().parents[2] / "grid5000" / ".node_info.json"
CREDS_FILE = Path.home() / ".python-grid5000.yaml"


def get_g5k_username() -> str:
    if yaml and CREDS_FILE.exists():
        with open(CREDS_FILE) as f:
            creds = yaml.safe_load(f) or {}
            if creds.get("username"):
                return creds["username"]
    return os.environ.get("USER", "g5kuser")


if not NODE_FILE.exists():
    print(json.dumps({"_meta": {"hostvars": {}}}))
    sys.exit(0)

info = json.loads(NODE_FILE.read_text())
node = info["node"]

# If this inventory is being executed ON the baremetal node itself
# (run_experiment.py calls ansible from the node), use a local connection
# instead of the round-trip laptop → access.g5k → node → access.g5k → node.
import re as _re
_my_fqdn = socket.getfqdn()
# True when this script is running ON a Grid5000 bare-metal node.
# G5K nodes always match <cluster>-<N>.<site>.grid5000.fr — a pattern no
# laptop will ever have — so this is more reliable than comparing against
# node_info.json (which may lag behind the current reservation).
ON_NODE = bool(_re.match(r"^[a-z]+-\d+\.[a-z]+\.grid5000\.fr$", _my_fqdn))

if ON_NODE:
    # Direct local connection — no SSH needed when already on the target machine.
    inventory = {
        "baremetal": {
            "hosts": ["localhost"],
            "vars": {
                "ansible_connection":         "local",
                "ansible_python_interpreter": "/usr/bin/python3",
            },
        },
        "_meta": {"hostvars": {"localhost": {}}},
    }
else:
    # Laptop path: single ProxyJump through the G5K access gateway.
    g5k_user   = get_g5k_username()
    proxy_jump = f"{g5k_user}@access.grid5000.fr"
    inventory = {
        "baremetal": {
            "hosts": [node],
            "vars": {
                "ansible_user":               "root",
                "ansible_python_interpreter": "/usr/bin/python3",
                "ansible_ssh_common_args":    f"-o StrictHostKeyChecking=no -o ProxyJump={proxy_jump}",
            },
        },
        "_meta": {"hostvars": {node: {}}},
    }

print(json.dumps(inventory, indent=2))
