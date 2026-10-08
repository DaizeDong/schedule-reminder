"""Check registry settings locally; sends nothing and never opens a database for writing."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/schedule-reminder/scripts"))
import config_lifecycle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="explicit registry file")
    parser.add_argument("--capabilities", default="store,remind")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = config_lifecycle.doctor(args.config, args.capabilities)
    except (OSError, ValueError, RuntimeError) as error:
        result = {"ready": False, "resolved_root": None, "problems": [str(error)]}
    if args.json:
        print(json.dumps(result, ensure_ascii=True))
    else:
        print("RESOLVED: " + str(result.get("resolved_root")))
        print("READY" if result["ready"] else "NOT READY")
        for problem in result["problems"]:
            print("- " + problem)
        print("Configuration only; use reminder.py health for measured capability readiness.")
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
