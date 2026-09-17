"""支持 `python -m mataof` 作为正式入口。"""

import sys

from mataof.cli import main

if __name__ == "__main__":
    sys.exit(main())
