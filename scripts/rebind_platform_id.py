from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.rebind import (  # noqa: E402
    RebindError,
    execute_rebind,
    plan_rebind,
    rollback_rebind,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="离线重绑定 Registry v2 managed records 的 platform_id"
    )
    parser.add_argument("--registry", required=True, help="webhook_tokens.json 路径")
    parser.add_argument("--source-platform-id")
    parser.add_argument("--destination-platform-id")
    parser.add_argument("--owner-user-id", help="可选精确 owner selector")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只读计划（默认）")
    mode.add_argument("--execute", action="store_true", help="执行 rebind")
    mode.add_argument("--rollback-manifest", help="使用 execute audit manifest 回滚")
    parser.add_argument("--manifest", help="execute/rollback audit 输出路径")
    parser.add_argument(
        "--confirm-offline",
        action="store_true",
        help="确认 AstrBot 与插件已停止；execute/rollback 必需",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.rollback_manifest:
            if not args.confirm_offline:
                raise RebindError("rollback 必须显式提供 --confirm-offline")
            result = rollback_rebind(
                args.registry,
                args.rollback_manifest,
                audit_path=args.manifest,
                confirm_offline=True,
            )
        else:
            if not args.source_platform_id or not args.destination_platform_id:
                raise RebindError(
                    "dry-run/execute 必须提供 source/destination platform_id"
                )
            if args.execute:
                if not args.confirm_offline:
                    raise RebindError("execute 必须显式提供 --confirm-offline")
                result = execute_rebind(
                    args.registry,
                    args.source_platform_id,
                    args.destination_platform_id,
                    args.owner_user_id,
                    args.manifest,
                    confirm_offline=True,
                )
            else:
                result = plan_rebind(
                    args.registry,
                    args.source_platform_id,
                    args.destination_platform_id,
                    args.owner_user_id,
                ).to_audit_summary()
                result = {"operation": "dry-run", **result}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except RebindError as exc:
        print(f"rebind 失败: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("rebind 失败: 未预期的内部错误", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
