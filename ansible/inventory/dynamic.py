#!/usr/bin/env python3
"""
Dynamic Ansible inventory — reads grid5000/.node_info.json.

Returns a single 'baremetal' group containing the reserved Grid5000 node.

SSH routing (single ProxyJump, built automatically from ~/.python-grid5000.yaml):
  laptop → access.grid5000.fr → node

Run as:  ansible-inventory -i ansible/inventory/dynamic.py --list
"""
import json
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
    # Fallback: read from the system user env
    import os
    return os.environ.get("USER", "g5kuser")


if not NODE_FILE.exists():
    print(json.dumps({"_meta": {"hostvars": {}}}))
    sys.exit(0)

info     = json.loads(NODE_FILE.read_text())
node     = info["node"]
g5k_user = get_g5k_username()

# Single ProxyJump through the G5K access gateway.
# access.grid5000.fr can resolve and reach node FQDNs (*.grid5000.fr) directly,
# so no intermediate site hop is needed for Ansible/SSH tunnelling.
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
    "_meta": {
        "hostvars": {node: {}}
    },
}
print(json.dumps(inventory, indent=2))
