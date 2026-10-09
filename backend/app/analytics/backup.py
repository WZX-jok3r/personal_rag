"""backup.py - 备份脚本（PG 两个库 + Qdrant 快照）（P7）。

## 为什么需要它

改造方案「不足」清单 C5：PG / Redis / Qdrant 均无备份策略与恢复演练。
`docker_setting/qdrant_storage` 是 bind mount，它只等于"没删容器"，**不是备份**
（误删、磁盘损坏、错误迁移都会一起带走）。

## 备份什么、怎么备

| 目标 | 方式 | 说明 |
|---|---|---|
| 系统元数据库 `rag` | `pg_dump -Fc`（自定义格式，可选择性恢复） | 会话/文档登记/审计/用量 |
| 业务库 `kb_analytics` | `pg_dump -Fc` | 可从 xlsx 重建，但重建要重跑 ETL |
| Qdrant | 官方 snapshot API | 逐 collection 快照 |
| 配置文件 | 只备份 `.env.example` 与清单 | **绝不备份 `.env`**（含 API Key） |

## 安全与运维要点

1. **默认写到 `backups/`（已 gitignore）**，文件名带 UTC 时间戳。
2. **绝不备份 `.env`** —— 那会把 API Key 与只读口令复制到备份目录，
   扩大泄露面。需要恢复配置时靠 `.env.example` + 密钥管理系统。
3. **保留策略**：默认保留最近 N 份（`--keep`），自动清理更早的，避免磁盘被撑满。
4. **恢复方式写进输出**，避免"有备份但不会恢复"。
   备份**只有经过恢复演练才算有效** —— 这是本项目对"备份"的态度。
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.core.config import settings

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = logging.getLogger(__name__)

BACKUP_ROOT = settings.base_dir / "backups"
POSTGRES_CONTAINER = "docker_setting-postgres-1"

# 恢复命令模板（打印给运维，避免"有备份不会恢复"）
RESTORE_HINTS = {
    "rag": "docker exec -i {c} pg_restore -U {u} -d {db} --clean --if-exists < {f}",
    "kb_analytics": "docker exec -i {c} pg_restore -U {u} -d {db} --clean --if-exists < {f}",
}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    logger.debug("[backup] 执行: %s", " ".join(cmd[:6]))
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def backup_postgres(dest: Path, databases: List[str]) -> List[Path]:
    """用 pg_dump -Fc 备份指定库（经 docker exec，复用容器内的 pg_dump 版本）。

    ⚠️ 实现要点：`pg_dump -Fc` 输出的是**二进制**，必须直接重定向到文件，
    绝不能用 `subprocess.run(..., text=True, capture_output=True)` 去接 ——
    那会按文本解码二进制流并抛 UnicodeDecodeError。
    初版就是先 text 模式试跑一次、再二进制写盘一次，导致**每个库 dump 两遍**
    并在 stderr 刷出吓人的解码异常（虽有备份产物，但日志噪音大且浪费一次全量导出）。
    """
    out: List[Path] = []
    for db in databases:
        target = dest / f"{db}_{_stamp()}.dump"
        # -Fc 自定义格式：支持并行恢复与选择性恢复，比纯 SQL 文本更实用
        cmd = [
            "docker", "exec", POSTGRES_CONTAINER,
            "pg_dump", "-U", settings.postgres_user, "-d", db, "-Fc",
        ]
        with open(target, "wb") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)

        if proc.returncode != 0:
            logger.error("[backup] pg_dump %s 失败: %s",
                         db, proc.stderr.decode("utf-8", "replace")[:300])
            target.unlink(missing_ok=True)
            continue

        size_kb = target.stat().st_size // 1024
        if size_kb < 1:
            # 空库 dump 也有几百字节；过小说明异常，保留文件但告警
            logger.warning("[backup] %s 备份文件过小（%d KB），请人工确认", db, size_kb)
        logger.info("[backup] PG %s -> %s (%d KB)", db, target.name, size_kb)
        out.append(target)
    return out


def backup_qdrant(dest: Path) -> List[Path]:
    """为每个 collection 创建 Qdrant 快照并记录快照名。

    用官方 snapshot API（而不是直接拷贝存储目录）：快照是一致性视图，
    直接拷目录可能拷到写一半的状态。
    """
    import requests

    base = f"http://{settings.qdrant_host}:{settings.qdrant_port}"
    out: List[Path] = []
    try:
        resp = requests.get(f"{base}/collections", timeout=15)
        resp.raise_for_status()
        names = [c["name"] for c in resp.json()["result"]["collections"]]
    except Exception as e:  # noqa: BLE001
        logger.error("[backup] 无法列出 Qdrant collections: %s", e)
        return out

    manifest_lines: List[str] = []
    for name in names:
        try:
            r = requests.post(
                f"{base}/collections/{name}/snapshots", timeout=300,
            )
            r.raise_for_status()
            snap = r.json()["result"]["name"]
            manifest_lines.append(f"{name}\t{snap}")
            logger.info("[backup] Qdrant %s 快照: %s", name, snap)
        except Exception as e:  # noqa: BLE001
            logger.error("[backup] Qdrant %s 快照失败: %s", name, str(e)[:200])

    if manifest_lines:
        mf = dest / f"qdrant_snapshots_{_stamp()}.txt"
        mf.write_text(
            "# Qdrant 快照清单（快照文件存放在 Qdrant 自己的存储目录内）\n"
            "# 恢复：POST /collections/{collection}/snapshots/recover  body={\"location\":\"file:///qdrant/snapshots/{collection}/{name}\"}\n"
            + "\n".join(manifest_lines) + "\n",
            encoding="utf-8",
        )
        out.append(mf)
    return out


def prune(dest_parent: Path, keep: int) -> List[Path]:
    """保留最近 keep 个备份目录，删除更早的。"""
    dirs = sorted([d for d in dest_parent.iterdir() if d.is_dir()], reverse=True)
    removed: List[Path] = []
    for d in dirs[keep:]:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d)
    return removed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="备份 PG（rag + kb_analytics）与 Qdrant 快照")
    ap.add_argument("--keep", type=int, default=7, help="保留最近 N 份备份（默认 7）")
    ap.add_argument("--out", type=str, default=str(BACKUP_ROOT), help="备份根目录")
    args = ap.parse_args()

    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / _stamp()
    dest.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print(f"📦 备份到 {dest}")
    print("=" * 72)

    dbs = [settings.postgres_db, settings.analytics_db]
    pg_files = backup_postgres(dest, dbs)
    qd_files = backup_qdrant(dest)

    print()
    print(f"PG 备份   : {len(pg_files)}/{len(dbs)} 成功")
    for f in pg_files:
        print(f"   {f.name}  ({f.stat().st_size // 1024} KB)")
    print(f"Qdrant    : {'快照清单已生成' if qd_files else '未生成（Qdrant 不可达？）'}")
    for f in qd_files:
        print(f"   {f.name}")

    # 恢复命令提示（避免"有备份不会恢复"）
    print()
    print("恢复方式（PG）:")
    for db in dbs:
        tmpl = RESTORE_HINTS["rag"]
        print("   " + tmpl.format(c=POSTGRES_CONTAINER, u=settings.postgres_user,
                                  db=db, f=f"<{db}_*.dump>"))
    print()
    print("⚠️ 本脚本**不备份 .env**（含 API Key 与口令）。"
          "配置请靠 .env.example + 密钥管理系统恢复。")
    print("⚠️ 备份只有经过**恢复演练**才算有效 —— 建议定期演练一次。")

    removed = prune(root, args.keep)
    if removed:
        print()
        print(f"🧹 已清理 {len(removed)} 个过期备份（保留最近 {args.keep} 份）")

    return 0 if pg_files else 1


if __name__ == "__main__":
    sys.exit(main())
