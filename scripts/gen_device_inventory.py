#!/usr/bin/env python3
"""生成通配设备清单（device_inventory）并做完整性校验。

为什么需要：设备分区计划按清单展开通配 FROM；清单缺设备时，合并结果会静默
少列（verify_merged 只按计划自身展开的子查询校验，发现不了）。因此清单必须
与数据库真实设备集合一致——本脚本先 SHOW DEVICES 收集，再 COUNT DEVICES 核对，
不一致直接报错退出、不写文件（绝不产出不完整清单）。

输出格式（与 mataof.runner.load_device_inventory 契合）：
    {"root.dbs.g_0.*": ["root.dbs.g_0.d_0", ...], ...}

用法：
  python3 scripts/gen_device_inventory.py \
      --out data/device_inventory.json \
      --pattern 'root.dbs.g_0.*' --pattern 'root.dbm.g_0.*' --pattern 'root.dbl.g_0.*'
  # 不传 --pattern 时使用上面三个默认模式（当前语料的通配 FROM）

退出码：0 成功；1 连接/读取失败；2 清单与数据库设备数不一致（未写文件）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile


DEFAULT_PATTERNS = (
    "root.dbs.g_0.*",
    "root.dbm.g_0.*",
    "root.dbl.g_0.*",
)


def _field_value(row) -> str:
    """RowRecord → 首列值（客户端不同版本 Field/原生值都兼容）。"""
    fields = row.get_fields() if hasattr(row, "get_fields") else []
    if not fields:
        return ""
    return str(fields[0])


def show_devices(session, pattern: str) -> list[str]:
    """SHOW DEVICES <pattern> → 去重排序后的设备路径。"""
    dataset = session.execute_query_statement(f"SHOW DEVICES {pattern}")
    try:
        devices = []
        while dataset.has_next():
            value = _field_value(dataset.next())
            if value:
                devices.append(value)
    finally:
        dataset.close_operation_handle()
    return sorted(set(devices))


def count_devices(session, pattern: str) -> int | None:
    """COUNT DEVICES <pattern> → 设备数；无结果 → None。"""
    dataset = session.execute_query_statement(f"COUNT DEVICES {pattern}")
    try:
        if not dataset.has_next():
            return None
        value = _field_value(dataset.next())
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    finally:
        dataset.close_operation_handle()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="data/device_inventory.json",
                   help="输出 JSON 路径（默认 data/device_inventory.json）")
    p.add_argument("--pattern", action="append", default=None,
                   help="通配 FROM 模式（可重复；缺省用 dbs/dbm/dbl 的 g_0.*）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6667)
    p.add_argument("--user", default="root")
    p.add_argument("--password", default="root")
    args = p.parse_args(argv)

    patterns = args.pattern or list(DEFAULT_PATTERNS)
    try:
        from iotdb.Session import Session
    except ImportError:
        print("错误：未安装 apache-iotdb 客户端（pip install apache-iotdb）",
              file=sys.stderr)
        return 1

    session = Session(args.host, args.port, args.user, args.password)
    try:
        try:
            session.open(False)
        except Exception as exc:
            print(f"错误：无法连接 IoTDB {args.host}:{args.port}：{exc}",
                  file=sys.stderr)
            return 1

        inventory: dict[str, list[str]] = {}
        mismatches: list[str] = []
        for pattern in patterns:
            devices = show_devices(session, pattern)
            actual = count_devices(session, pattern)
            if not devices or actual is None or actual != len(devices):
                mismatches.append(
                    f"{pattern}: SHOW {len(devices)} 台 / COUNT {actual} 台")
                continue
            inventory[pattern] = devices
            print(f"[inventory] {pattern}: {len(devices)} 台 ✓")

        if mismatches:
            print("错误：以下模式的清单与数据库设备数不一致，未写文件：",
                  file=sys.stderr)
            for item in mismatches:
                print(f"  - {item}", file=sys.stderr)
            return 2

        out_dir = os.path.dirname(os.path.abspath(args.out))
        os.makedirs(out_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".device_inventory-", suffix=".json",
                                   dir=out_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(inventory, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, args.out)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        total = sum(len(v) for v in inventory.values())
        print(f"[inventory] 写入 {args.out}：{len(inventory)} 个模式 / "
              f"{total} 台设备")
        return 0
    finally:
        try:
            session.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
