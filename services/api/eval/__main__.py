"""``python -m eval`` prints the entry points."""

from __future__ import annotations


def main() -> None:
    print(
        "Routing eval\n"
        "  python -m eval.run --suite smoke\n"
        "  python -m eval.run --suite fast\n"
        "  python -m eval.sensitivity --suite fast\n"
        "  python -m eval.improve --dry-run\n"
        "  python -m eval.report --run <results.json>\n"
    )


if __name__ == "__main__":
    main()
