#!/usr/bin/env python3
"""
reserve.py — Reserve a Grid5000 bare-metal node for the experiment.

Run from your laptop:
  pip install enoslib
  python3 grid5000/reserve.py --site rennes --walltime 03:00:00

Pin to the SAME node across runs (for hardware reproducibility):
  # First run: let Grid5000 pick any node → saves hostname to .node_info.json
  python3 grid5000/reserve.py --site rennes

  # All subsequent runs: pin to that exact node
  python3 grid5000/reserve.py --site rennes \
    --node $(python3 -c "import json; print(json.load(open('grid5000/.node_info.json'))['node'])")

On success:
  - Prints the node hostname
  - Saves node info to grid5000/.node_info.json (read by the Ansible inventory)

Available sites:
  rennes    (paravance, parasilo, taillante)
  nancy     (gros, graffiti)
  lyon      (troll, gemini)
  sophia    (uvb)
  grenoble  (dahu)
  bordeaux  (abacus)
  lille     (chifflot)
  nantes    (econome)
  luxembourg (fragarin)

Check live availability: https://www.grid5000.fr/w/Status
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import enoslib as en
except ImportError:
    print("enoslib not installed.  Run:  pip install enoslib")
    sys.exit(1)

OUTPUT_FILE = Path(__file__).parent / ".node_info.json"

# Default cluster per site — well-maintained, KVM-capable, modern hardware.
SITE_DEFAULTS = {
    "rennes":     "parasilo",    # Intel Xeon E5-2630 v3, 16c/32t, 128 GB — paravance retired
    "nancy":      "gros",
    "lyon":       "troll",
    "sophia":     "uvb",
    "grenoble":   "dahu",
    "bordeaux":   "abacus",
    "lille":      "chifflot",
    "nantes":     "econome",
    "luxembourg": "fragarin",
}


def main():
    parser = argparse.ArgumentParser(
        description="Reserve a Grid5000 node for CorrectExam experiments",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--site",
        default="rennes",
        choices=list(SITE_DEFAULTS.keys()),
        help="Grid5000 site to reserve on",
    )
    parser.add_argument(
        "--cluster",
        default=None,
        help="Cluster name within the site (default: site default)",
    )
    parser.add_argument(
        "--node",
        default=None,
        metavar="FQDN",
        help=(
            "Pin reservation to a specific node FQDN for hardware reproducibility. "
            "Example: paravance-1.rennes.grid5000.fr  "
            "After the first run, reuse: "
            "--node $(python3 -c \"import json; print(json.load(open('grid5000/.node_info.json'))['node'])\")"
        ),
    )
    parser.add_argument(
        "--walltime",
        default="03:00:00",
        help="Job duration HH:MM:SS (Phase 1 only: 05:00:00, full matrix: 08:00:00)",
    )
    parser.add_argument("--job-name", default="correctexam-perf")
    args = parser.parse_args()

    cluster = args.cluster or SITE_DEFAULTS[args.site]

    if args.node:
        print(f"Pinning to node:  {args.node}")
        print(f"Site/cluster:     {args.site}/{cluster}  ({args.walltime})\n")
        print("(This blocks until the node is granted — may take longer when pinning.)\n")
    else:
        print(f"Requesting 1 node on {args.site}/{cluster} for {args.walltime} …")
        print("(This blocks until the node is granted — typically < 5 min for off-peak.)\n")

    # Build OAR properties: optionally pin to a specific node hostname.
    oar_properties = None
    if args.node:
        # OAR resource property filter — forces scheduler to assign this exact node.
        oar_properties = f"hostname='{args.node}'"

    conf_builder = (
        en.G5kConf.from_settings(
            job_name=args.job_name,
            walltime=args.walltime,
            # allow_classic_ssh: keep the default OS environment (KVM available,
            # no custom image deploy needed).
            job_type=["allow_classic_ssh"],
        )
        .add_machine(
            roles=["baremetal"],
            cluster=cluster,
            nodes=1,
            **({"oar_properties": oar_properties} if oar_properties else {}),
        )
        .finalize()
    )

    provider = en.G5k(conf_builder)
    roles, _networks = provider.init()

    node     = roles["baremetal"][0]
    node_addr = node.address

    info = {
        "site":      args.site,
        "cluster":   cluster,
        "node":      node_addr,
        "walltime":  args.walltime,
        "job_name":  args.job_name,
        "pinned":    args.node is not None,
    }
    OUTPUT_FILE.write_text(json.dumps(info, indent=2))

    print(f"Node reserved:  {node_addr}")
    print(f"SSH:            ssh root@{node_addr}")
    print(f"Info saved to:  {OUTPUT_FILE}\n")

    # Print the pin command so the user can reuse the same node next time.
    if not args.node:
        print(f"To reuse this exact node next time (hardware reproducibility):")
        print(f"  python3 grid5000/reserve.py --site {args.site} --node {node_addr}\n")

    print("Next steps (automated via Ansible):")
    print(f"  ansible-playbook -i ansible/inventory/dynamic.py \\")
    print(f"    ansible/playbooks/setup_node.yml")
    print(f"  ssh root@{node_addr} \\")
    print(f"    'cd /root/CorrigeExam && python3 grid5000/scripts/run_experiment.py --all'")
    print(f"  ansible-playbook -i ansible/inventory/dynamic.py \\")
    print(f"    ansible/playbooks/collect_results.yml")


if __name__ == "__main__":
    main()
