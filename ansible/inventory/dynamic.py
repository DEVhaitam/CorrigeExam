#!/usr/bin/env python3
"""
Dynamic Ansible inventory — reads grid5000/.node_info.json.

Returns a single 'baremetal' group containing the reserved Grid5000 node.
Run as:  ansible-inventory -i ansible/inventory/dynamic.py --list
"""
import json
import sys
from pathlib import Path

NODE_FILE = Path(__file__).resolve().parents[2] / "grid5000" / ".node_info.json"

if not NODE_FILE.exists():
    print(json.dumps({"_meta": {"hostvars": {}}}))
    sys.exit(0)

info  = json.loads(NODE_FILE.read_text())
node  = info["node"]

inventory = {
    "baremetal": {
        "hosts": [node],
        "vars": {
            "ansible_user":               "root",
            "ansible_python_interpreter": "/usr/bin/python3",
        },
    },
    "_meta": {
        "hostvars": {node: {}}
    },
}
print(json.dumps(inventory, indent=2))
