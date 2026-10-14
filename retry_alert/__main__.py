import sys

from ._bootstrap import ensure_interpreter

ensure_interpreter(__file__)

from .cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
