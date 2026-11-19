# -*- coding: utf-8 -*-
"""将 MindSpider 导出 CSV upsert 到达梦 mindspider_war。"""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from loguru import logger

from crawl_import.config import batch_size, db_config, target_schema

try:
    import dmPython
except ImportError:  # pragma: no cover
    dmPython = None

# 业务主键：用于幂等 upsert（忽略自增 id）
TABLE_PK: Dict[str, str] = {
    "weibo_note": "note_id",
    "weibo_note_comment": "comment_id",
    "weibo_creator": "user_id",
    "xhs_note": "note_id",
    "xhs_note_comment": "comment_id",
    "xhs_creator": "user_id",
    "douyin_aweme": "aweme_id",
    "douyin_aweme_comment": "comment_id",
    "dy_creator": "user_id",
    "kuaishou_video": "video_id",
    "kuaishou_video_comment": "comment_id",
    "bilibili_video": "video_id",
    "bilibili_video_comment": "comment_id",
    "bilibili_up_info": "user_id",
    "tieba_note": "note_id",
    "tieba_comment": "comment_id",
    "tieba_creator": "user_id",
    "zhihu_content": "content_id",
    "zhihu_comment": "comment_id",
    "zhihu_creator": "user_id",
}

ProgressCb = Callable[[Dict[str, Any]], None]


def extract_zip(archive: Path, dest_dir: Path) -> Path:
    """解压 zip 到 dest_dir，返回含 csv/manifest 的内容目录。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(dest_dir)

    # 常见：dest/task_xxx/*.csv 或 dest/*.csv
    if any(dest_dir.glob("*.csv")) or (dest_dir / "manifest.json").exists():
        return dest_dir
    subdirs = [p for p in dest_dir.iterdir() if p.is_dir() and not p.name.startswith("_")]
    if len(subdirs) == 1:
        return subdirs[0]
    for sub in subdirs:
        if any(sub.glob("*.csv")) or (sub / "manifest.json").exists():
            return sub
    return dest_dir


def discover_csv_tables(content_dir: Path) -> List[Path]:
    files = sorted(content_dir.glob("*.csv"))
    return [p for p in files if p.stem in TABLE_PK]


def _schema_candidates(raw: str) -> List[str]:
    """达梦未加引号标识符常存为大写；返回去重后的候选 schema 名。"""
    base = (raw or "").strip()
    out: List[str] = []
    for v in (base, base.upper(), base.lower()):
        if v and v not in out:
            out.append(v)
    return out


def _try_alter_schema(cur, conn, schema: str) -> bool:
    for ddl in (
        f'ALTER SESSION SET CURRENT_SCHEMA="{schema}"',
        f"ALTER SESSION SET CURRENT_SCHEMA={schema}",
    ):
        try:
            cur.execute(ddl)
            try:
                conn.commit()
            except Exception:
                pass
            return True
        except Exception:
            continue
    return False


def _canonical_schema(cur, schema: str) -> Optional[str]:
    """
    返回库中真实 OWNER 大小写。
    注意：ALL_TABLES 用 UPPER 能命中，不代表 '"mindspider_war"' 限定名可用。
    """
    try:
        cur.execute(
            "SELECT OWNER FROM ALL_TABLES WHERE UPPER(OWNER)=UPPER(?) "
            "FETCH FIRST 1 ROWS ONLY",
            (schema,),
        )
        row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass

    # 系统视图不可用：直接试限定/非限定读表
    for candidate in _schema_candidates(schema):
        for table in ("weibo_note", "WEIBO_NOTE", "zhihu_content", "ZHIHU_CONTENT"):
            for sql in (
                f'SELECT * FROM "{candidate}"."{table}" WHERE 1=0',
                f'SELECT * FROM "{table}" WHERE 1=0',
            ):
                try:
                    cur.execute(sql)
                    return candidate
                except Exception:
                    continue
    return None


def _connect():
    if dmPython is None:
        raise RuntimeError("dmPython 未安装，无法写入达梦")
    cfg = db_config()
    connect_kwargs = dict(
        user=cfg["user"],
        password=cfg["password"],
        server=cfg["host"],
        port=cfg["port"],
    )
    try:
        conn = dmPython.connect(**connect_kwargs, local_code=1)
    except TypeError:
        conn = dmPython.connect(**connect_kwargs)
    cur = conn.cursor()
    configured = target_schema()
    errors: List[str] = []
    for schema in _schema_candidates(configured):
        if not _try_alter_schema(cur, conn, schema):
            errors.append(f"ALTER SESSION 失败: {schema}")
            continue
        canonical = _canonical_schema(cur, schema)
        if not canonical:
            errors.append(f"schema 下无表或不可读: {schema}")
            continue
        if canonical != schema:
            _try_alter_schema(cur, conn, canonical)
        # 最终用真实 OWNER 再验证一次限定查询
        probe_ok = False
        for table in ("weibo_note", "WEIBO_NOTE", "zhihu_content", "ZHIHU_CONTENT"):
            try:
                cur.execute(f'SELECT * FROM "{canonical}"."{table}" WHERE 1=0')
                probe_ok = True
                break
            except Exception:
                try:
                    cur.execute(f'SELECT * FROM "{table}" WHERE 1=0')
                    probe_ok = True
                    break
                except Exception as e:
                    errors.append(f"{canonical}.{table}: {e}")
        if probe_ok:
            logger.info(
                "crawl_import 使用达梦 schema={} (配置={})",
                canonical,
                configured,
            )
            return conn, cur, canonical
        errors.append(f"schema 限定查询失败: {canonical}")
    raise RuntimeError(
        f"无法切换/访问达梦 schema={configured!r}；"
        f"请确认 CRAWL_IMPORT_SCHEMA / DM_CURRENT_SCHEMA。"
        + ((" 细节: " + " | ".join(errors[:6])) if errors else "")
    )


def _table_ref(schema: str, table: str) -> str:
    return f'"{schema}"."{table}"'


def _resolve_table(cur, schema: str, table: str) -> Tuple[str, List[str]]:
    """返回 (实际可用表名, 列名列表)。"""
    last_err: Optional[Exception] = None
    for tname in (table, table.upper(), table.lower()):
        for sql in (
            f"SELECT * FROM {_table_ref(schema, tname)} WHERE 1=0",
            f'SELECT * FROM "{tname}" WHERE 1=0',
        ):
            try:
                cur.execute(sql)
                cols = [d[0] for d in (cur.description or [])]
                if cols:
                    return tname, cols
            except Exception as e:
                last_err = e
                continue
    raise RuntimeError(last_err or f"无法读取表 {schema}.{table}")


def _normalize_value(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if text == "" or text.lower() in {"null", "none", "nan"}:
            return None
        return text
    return raw


def _row_exists(cur, schema: str, table: str, pk: str, pk_val: Any) -> bool:
    sql = f'SELECT 1 FROM "{schema}"."{table}" WHERE "{pk}"=? FETCH FIRST 1 ROWS ONLY'
    cur.execute(sql, (pk_val,))
    return cur.fetchone() is not None


def _insert_row(
    cur,
    schema: str,
    table: str,
    columns: Sequence[str],
    values: Sequence[Any],
) -> None:
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["?"] * len(columns))
    sql = f'INSERT INTO "{schema}"."{table}" ({cols_sql}) VALUES ({placeholders})'
    cur.execute(sql, tuple(values))


def _update_row(
    cur,
    schema: str,
    table: str,
    pk: str,
    columns: Sequence[str],
    values: Sequence[Any],
    pk_val: Any,
) -> None:
    set_sql = ", ".join(f'"{c}"=?' for c in columns)
    sql = f'UPDATE "{schema}"."{table}" SET {set_sql} WHERE "{pk}"=?'
    cur.execute(sql, tuple(values) + (pk_val,))


def import_csv_file(
    cur,
    conn,
    schema: str,
    csv_path: Path,
    *,
    on_batch: Optional[Callable[[int, int, int], None]] = None,
) -> Dict[str, Any]:
    logical_name = csv_path.stem
    pk = TABLE_PK.get(logical_name)
    if not pk:
        return {
            "name": logical_name,
            "skipped": True,
            "message": "未配置主键，跳过",
            "rows_read": 0,
            "rows_inserted": 0,
            "rows_updated": 0,
            "rows_skipped": 0,
        }

    try:
        table, db_cols = _resolve_table(cur, schema, logical_name)
    except Exception as e:
        return {
            "name": logical_name,
            "skipped": True,
            "message": f"表不存在或不可读: {e}",
            "rows_read": 0,
            "rows_inserted": 0,
            "rows_updated": 0,
            "rows_skipped": 0,
        }

    writable = [c for c in db_cols if c.lower() != "id"]
    writable_set = {c.lower(): c for c in writable}

    rows_read = rows_ins = rows_upd = rows_skip = 0
    batch = batch_size()

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return {
                "name": logical_name,
                "db_table": table,
                "rows_read": 0,
                "rows_inserted": 0,
                "rows_updated": 0,
                "rows_skipped": 0,
                "message": "空 CSV",
            }

        csv_to_db: List[Tuple[str, str]] = []
        for field in reader.fieldnames:
            key = (field or "").strip()
            if not key:
                continue
            real = writable_set.get(key.lower())
            if real:
                csv_to_db.append((key, real))

        if pk.lower() not in {c.lower() for _, c in csv_to_db}:
            return {
                "name": logical_name,
                "db_table": table,
                "skipped": True,
                "message": f"CSV 缺少主键列 {pk}",
                "rows_read": 0,
                "rows_inserted": 0,
                "rows_updated": 0,
                "rows_skipped": 0,
            }

        for row in reader:
            rows_read += 1
            mapped: Dict[str, Any] = {}
            for csv_key, db_col in csv_to_db:
                mapped[db_col] = _normalize_value(row.get(csv_key))

            if not any(c.lower() == pk.lower() for c in mapped):
                rows_skip += 1
                continue

            # 统一用真实列名取 pk
            pk_col = next(c for c in mapped if c.lower() == pk.lower())
            pk_val = mapped[pk_col]
            if pk_val is None:
                rows_skip += 1
                continue
            cols = list(mapped.keys())
            vals = [mapped[c] for c in cols]

            try:
                if _row_exists(cur, schema, table, pk_col, pk_val):
                    upd_cols = [c for c in cols if c.lower() != pk.lower()]
                    upd_vals = [mapped[c] for c in upd_cols]
                    if upd_cols:
                        _update_row(cur, schema, table, pk_col, upd_cols, upd_vals, pk_val)
                    rows_upd += 1
                else:
                    _insert_row(cur, schema, table, cols, vals)
                    rows_ins += 1
            except Exception as e:
                rows_skip += 1
                logger.warning("导入行失败 table={} pk={} err={}", table, pk_val, e)
                try:
                    conn.rollback()
                except Exception:
                    pass
                continue

            if (rows_ins + rows_upd) % batch == 0:
                conn.commit()
                if on_batch:
                    on_batch(rows_read, rows_ins, rows_upd)

        conn.commit()

    return {
        "name": logical_name,
        "db_table": table,
        "rows_read": rows_read,
        "rows_inserted": rows_ins,
        "rows_updated": rows_upd,
        "rows_skipped": rows_skip,
        "message": "ok",
    }


def import_content_dir(
    content_dir: Path,
    *,
    progress_cb: Optional[ProgressCb] = None,
) -> Dict[str, Any]:
    csv_files = discover_csv_tables(content_dir)
    if not csv_files:
        raise RuntimeError(f"目录中未找到可识别的 CSV: {content_dir}")

    conn, cur, schema = _connect()
    table_stats: List[Dict[str, Any]] = []
    try:
        total = len(csv_files)
        for idx, csv_path in enumerate(csv_files, start=1):
            if progress_cb:
                progress_cb(
                    {
                        "phase": "importing",
                        "message": f"正在导入 {csv_path.name} ({idx}/{total})",
                        "progress_percent": int(10 + 80 * (idx - 1) / max(total, 1)),
                        "tables": table_stats,
                    }
                )

            def _batch_hook(read_n, ins_n, upd_n, name=csv_path.name):
                if progress_cb:
                    progress_cb(
                        {
                            "phase": "importing",
                            "message": f"{name}: read={read_n} insert={ins_n} update={upd_n}",
                            "tables": table_stats
                            + [
                                {
                                    "name": csv_path.stem,
                                    "rows_read": read_n,
                                    "rows_inserted": ins_n,
                                    "rows_updated": upd_n,
                                }
                            ],
                        }
                    )

            stat = import_csv_file(cur, conn, schema, csv_path, on_batch=_batch_hook)
            table_stats.append(stat)
            logger.info("导入完成 {}", stat)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    written = sum(
        int(t.get("rows_inserted") or 0) + int(t.get("rows_updated") or 0)
        for t in table_stats
    )
    if written <= 0:
        detail = "; ".join(
            f"{t.get('name')}: {t.get('message') or 'no writes'}"
            for t in table_stats[:8]
        )
        if progress_cb:
            progress_cb(
                {
                    "phase": "failed",
                    "message": "入库未写入任何行",
                    "tables": table_stats,
                    "schema": schema,
                    "progress_percent": 100,
                }
            )
        raise RuntimeError(
            f"入库未写入任何行（schema={schema}）。"
            f"请检查达梦 schema/表是否存在。详情: {detail}"
        )

    return {
        "schema": schema,
        "tables": table_stats,
        "csv_count": len(csv_files),
        "rows_written": written,
    }
