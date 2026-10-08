"""Create a blank registry in an already initialized PRIVATE companion."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/schedule-reminder/scripts"))
import config_lifecycle
import private_data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        path, created = config_lifecycle.initialize(args.out or private_data.config_root())
    except (OSError, ValueError, RuntimeError) as error:
        print("NOT READY: " + str(error))
        return 1
    print(("Created blank registry: " if created else "Preserved existing registry: ") + str(path))
    print("Fill the selected destinations, then run tools/verify_config.py. Database init remains separate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
