"""Derive profiles from the downloaded Apache-2.0 Moby sources."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent
profile = json.loads((root / "seccomp-upstream.json").read_text())
profile["syscalls"].append({
    "names": ["clone", "unshare", "mount", "umount", "umount2", "pivot_root", "setns"],
    "action": "SCMP_ACT_ALLOW",
    "comment": "Allow nested bubblewrap namespaces. This broadens kernel attack surface; capabilities inside new user namespaces differ from host capabilities.",
})
(root / "codexpp-seccomp.json").write_text(json.dumps(profile, indent=2) + "\n")
template = (root / "apparmor-upstream.go").read_text().split('const baseTemplate = `', 1)[1].split('`', 1)[0]
start = template.index("  network,")
body = template[start:]
body = body.replace("{{.DaemonProfile}}", "unconfined").replace("{{.PeerName}}", "codexpp-sandbox")
body = body.replace("  deny mount,", "  # Modified for nested namespaces: userns/mount/pivot_root broaden kernel attack surface.\n  userns,\n  mount,\n  pivot_root,")
text = ("# SPDX-License-Identifier: Apache-2.0\n# Copyright The Moby Authors\n# Modified by codex++ for nested sandbox namespaces.\n"
        "abi <abi/4.0>,\ninclude <tunables/global>\n"
        "profile codexpp-sandbox flags=(attach_disconnected,mediate_deleted) {\n"
        "  include <abstractions/base>\n  network unix,\n" + body)
(root / "codexpp.apparmor").write_text(text)
print("Generated codexpp seccomp and AppArmor profiles")
