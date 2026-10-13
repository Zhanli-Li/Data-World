#!/usr/bin/env python3
"""兼容入口；实现位于可维护的 ``data_world`` 包。"""

from data_world.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
