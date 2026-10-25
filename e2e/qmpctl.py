"""CLI: qmpctl.py SOCKET COMMAND [JSON-ARGS]  (prints the JSON return value)."""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2e.qmp import QMP, QMPError  # noqa: E402


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    sock, cmd = argv[1], argv[2]
    args = json.loads(argv[3]) if len(argv) > 3 else {}
    try:
        with QMP(sock, timeout=20) as q:
            print(json.dumps(q.command(cmd, **args)))
    except (QMPError, OSError) as e:
        print(f"qmpctl: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
