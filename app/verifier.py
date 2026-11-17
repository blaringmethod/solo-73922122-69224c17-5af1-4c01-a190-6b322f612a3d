"""Verify generated SQL by actually loading it into an isolated empty database.

For every job a fresh database is created on the verification cluster, the
three SQL files run inside their own transactions, and afterwards negative
probes prove that primary keys, unique keys and foreign keys really reject
violations. The verification database is always dropped afterwards. No
partial package is ever exposed on failure.
"""
from __future__ import annotations

import json
import secrets

import psycopg

from .config import settings
from .introspect import IntrospectionError, introspect_tables
from .models import TYPE_BOUNDS, ForeignKey, TableSchema
from .sqlgen import q


class VerificationError(Exception):
    pass


# Sentinel values guaranteed never to appear in solver output: negative and
# below every requested interval, but kept inside each column's type bounds so
# the rejection comes from the constraint (23503/23505), not a type overflow.
def _sentinel(col, offset: int) -> int:
    low = TYPE_BOUNDS[col.pg_type][0]
    return low + 1 + offset


def _execute_script(conn, script: str, log: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(script)
    conn.commit()
    log.append(f"executed script ({len(script)} bytes)")


def _run_negative(conn, sql: str, log: list[str], label: str, expect_sqlstate: str | None = None) -> None:
    """Run SQL inside a savepoint; it must be rejected by a constraint."""
    savepoint = f"sp_{secrets.token_hex(6)}"
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {q(savepoint)}")
        try:
            cur.execute(sql)
        except psycopg.Error as exc:
            cur.execute(f"ROLLBACK TO SAVEPOINT {q(savepoint)}")
            conn.commit()
            if expect_sqlstate and exc.sqlstate != expect_sqlstate:
                raise VerificationError(
                    f"负向校验异常：{label} 期望 {expect_sqlstate}，实际 {exc.sqlstate} ({exc})"
                )
            log.append(f"negative probe OK [{label}] -> {exc.sqlstate}")
            return
        cur.execute(f"ROLLBACK TO SAVEPOINT {q(savepoint)}")
        conn.commit()
    raise VerificationError(f"负向校验失败：{label} 本应被约束拒绝，但执行成功")


def verify_package(
    admin_dsn: str,
    db_prefix: str,
    schema: str,
    tables: dict[str, TableSchema],
    selected: list[str],
    package: dict[str, str],
) -> tuple[bool, list[str]]:
    db_name = f"{db_prefix}{secrets.token_hex(8)}"
    log: list[str] = [f"creating isolated database {db_name}"]
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10) as adm:
            with adm.cursor() as cur:
                cur.execute(f"CREATE DATABASE {q(db_name)}")
        dsn = _replace_dbname(admin_dsn, db_name)
        with psycopg.connect(dsn, autocommit=False, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {q(schema)}")
            conn.commit()
            _execute_script(conn, package["01_schema.sql"], log)
            _execute_script(conn, package["02_data.sql"], log)
            _execute_script(conn, package["03_constraints.sql"], log)
            _assert_row_counts(conn, schema, selected, log)
            # The deployed constraints must be structurally identical to the
            # source catalog (same columns, MATCH type, actions, including any
            # ON DELETE SET NULL column list). A silently-dropped column list
            # would otherwise only surface as a wrong action at runtime.
            _assert_constraints_match_source(conn, schema, tables, selected, log)
            before = _snapshot(conn, schema, tables, selected)
            _assert_constraints_enforced(conn, schema, tables, selected, log)
            _assert_partial_set_null_actions(conn, schema, tables, selected, log)
            after = _snapshot(conn, schema, tables, selected)
            if before != after:
                raise VerificationError(
                    "探测改变了隔离库中的最终样本（装载数据在验证前后不一致）"
                )
            log.append("sample snapshot unchanged by negative/semantic probes")
        log.append("all verification steps passed")
        return True, log
    except (psycopg.Error, VerificationError, KeyError) as exc:
        log.append(f"verification FAILED: {exc.__class__.__name__}: {exc}")
        return False, log
    finally:
        _cleanup_database(admin_dsn, db_name, log)


def _cleanup_database(admin_dsn: str, db_name: str, log: list[str]) -> None:
    """Terminate backends and release the verification database.

    Standard mode (``VERIFY_CLEANUP=drop``, the default and the behaviour in
    the shipped docker image) runs ``DROP DATABASE``.

    Sandboxed kernels may deny the signal used during the drop's checkpoint;
    ``VERIFY_CLEANUP=detach`` then disconnects every backend and renames the
    database aside instead of dropping it, so the environment limitation can
    never mask a verification verdict.
    """
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10) as adm:
            with adm.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s
                      AND pid IS NOT NULL
                      AND pid <> pg_backend_pid()
                    """,
                    (db_name,),
                )
                if settings.verify_cleanup == "detach":
                    retired = f"retired_{db_name}"
                    cur.execute(
                        f"ALTER DATABASE {q(db_name)} RENAME TO {q(retired)}"
                    )
                    log.append(
                        f"detached verification database (renamed to {retired}); "
                        f"set VERIFY_CLEANUP=drop on a standard postgres image to drop it"
                    )
                else:
                    cur.execute(f"DROP DATABASE IF EXISTS {q(db_name)}")
                    log.append(f"dropped verification database {db_name}")
    except psycopg.Error as exc:
        log.append(
            f"cleanup warning: verification verdict is unaffected; "
            f"could not release {db_name} ({exc.sqlstate}: {exc})."
        )


def _replace_dbname(dsn: str, dbname: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    info["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**info)


def _assert_row_counts(conn, schema, selected, log) -> None:
    with conn.cursor() as cur:
        for tname in selected:
            cur.execute(f"SELECT count(*) FROM {q(schema)}.{q(tname)}")
            (n,) = cur.fetchone()
            log.append(f"row count {tname} = {n}")


def _first_row(conn, schema, tname, cols):
    cols_sql = ", ".join(q(c) for c in cols)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {cols_sql} FROM {q(schema)}.{q(tname)} LIMIT 1")
        return cur.fetchone()


def _assert_constraints_match_source(
    conn, schema: str, tables: dict[str, TableSchema], selected: list[str], log: list[str]
) -> None:
    """Compare the loaded catalogs against the source structure field by field.

    This catches a rendered constraint that is merely *accepted* by PostgreSQL
    but semantically different from the source — most importantly an
    ``ON DELETE SET NULL (col, ...)`` column list that was dropped, which would
    silently turn into a whole-key SET NULL.
    """
    try:
        actual = introspect_tables(conn, schema, selected)
    except IntrospectionError as exc:
        raise VerificationError(f"装载后的目录无法内省: {exc}") from exc
    for tname in selected:
        want, got = tables[tname], actual[tname]
        want_cols = [(c.name, c.pg_type, c.nullable) for c in want.columns]
        got_cols = [(c.name, c.pg_type, c.nullable) for c in got.columns]
        if want_cols != got_cols:
            raise VerificationError(
                f"目录不一致：{tname} 列定义期望 {want_cols}，装载后为 {got_cols}"
            )
        want_uk = {
            (u.name, tuple(u.columns), u.is_primary) for u in want.unique_constraints
        }
        got_uk = {
            (u.name, tuple(u.columns), u.is_primary) for u in got.unique_constraints
        }
        if want_uk != got_uk:
            raise VerificationError(
                f"目录不一致：{tname} 主键/唯一键期望 {sorted(want_uk)}，装载后为 {sorted(got_uk)}"
            )
        want_fk = {f.name: f for f in want.foreign_keys}
        got_fk = {f.name: f for f in got.foreign_keys}
        if set(want_fk) != set(got_fk):
            raise VerificationError(
                f"目录不一致：{tname} 外键期望 {sorted(want_fk)}，装载后为 {sorted(got_fk)}"
            )
        for name, wf in want_fk.items():
            gf = got_fk[name]
            fields = (
                "columns",
                "ref_table",
                "ref_columns",
                "match",
                "on_update",
                "on_delete",
                "on_delete_set_columns",
                "not_deferrable",
                "initially_deferred",
            )
            for field in fields:
                if getattr(wf, field) != getattr(gf, field):
                    raise VerificationError(
                        f"目录不一致：{tname}.{name}.{field} 期望 "
                        f"{getattr(wf, field)!r}，装载后为 {getattr(gf, field)!r}"
                    )
        log.append(f"catalog constraints match source for {tname}")


def _assert_constraints_enforced(conn, schema, tables, selected, log) -> None:
    """Negative probes proving constraints are live, not merely present.

    Probes use UPDATE inside savepoints on existing rows, so they neither
    depend on inserting a full valid row nor mutate the verified data. The
    caller re-checks the row snapshot afterwards to prove that the probes
    changed nothing.
    """
    for tname in selected:
        ts = tables[tname]
        first = _first_row(conn, schema, tname, [c.name for c in ts.columns])
        if first is None:
            log.append(f"skip negative probes for {tname} (0 rows)")
            continue
        col_by_name = {c.name: c for c in ts.columns}
        tbl = f"{q(schema)}.{q(tname)}"
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {tbl}")
            (n_rows,) = cur.fetchone()

        # --- PK / UNIQUE: force the first row's key onto a second row's key ---
        for uc in ts.unique_constraints:
            kind = "主键" if uc.is_primary else "唯一键"
            if n_rows < 2:
                log.append(f"skip {kind} probe {uc.name}: table has fewer than 2 rows")
                continue
            key_cols = uc.columns
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(q(c) for c in key_cols)} "
                    f"FROM {tbl} "
                    f"WHERE {' AND '.join(q(c) + ' IS NOT NULL' for c in key_cols)} "
                    f"LIMIT 1"
                )
                keyvals = cur.fetchone()
            if keyvals is None:
                log.append(f"skip {kind} probe {uc.name}: no non-NULL key row")
                continue
            set_clause = ", ".join(
                f"{q(c)} = {_lit(v)}" for c, v in zip(key_cols, keyvals)
            )
            # Apply to every *other* row; with >=2 rows a duplicate is forced.
            _run_negative(
                conn,
                f"UPDATE {tbl} SET {set_clause} WHERE ctid NOT IN "
                f"(SELECT ctid FROM {tbl} LIMIT 1)",
                log,
                f"{tname} {kind} {uc.name} 重复",
                expect_sqlstate="23505",
            )

        # --- Foreign keys: point an existing row at a guaranteed-absent key ---
        for fk in ts.foreign_keys:
            set_parts = []
            for k, cname in enumerate(fk.columns):
                col = col_by_name[cname]
                set_parts.append(f"{q(cname)} = {_sentinel(col, k)}")
            set_clause = ", ".join(set_parts)
            _run_negative(
                conn,
                f"UPDATE {tbl} SET {set_clause} WHERE ctid IN "
                f"(SELECT ctid FROM {tbl} LIMIT 1)",
                log,
                f"{tname} 外键 {fk.name} 悬空引用",
                expect_sqlstate="23503",
            )

            if len(fk.columns) > 1:
                # MATCH FULL must reject mixed NULL/non-NULL composite keys.
                mixed_parts = []
                for k, cname in enumerate(fk.columns):
                    if k == 0:
                        mixed_parts.append(f"{q(cname)} = NULL")
                    else:
                        col = col_by_name[cname]
                        mixed_parts.append(f"{q(cname)} = {_sentinel(col, 100 + k)}")
                mixed_sql = (
                    f"UPDATE {tbl} SET {', '.join(mixed_parts)} WHERE ctid IN "
                    f"(SELECT ctid FROM {tbl} LIMIT 1)"
                )
                if fk.match.value == "MATCH FULL":
                    _run_negative(
                        conn,
                        mixed_sql,
                        log,
                        f"{tname} 外键 {fk.name} MATCH FULL 混合NULL",
                        expect_sqlstate="23503",
                    )
                else:
                    # MATCH SIMPLE allows a mixed-NULL key (no FK violation).
                    savepoint = f"sp_{secrets.token_hex(6)}"
                    with conn.cursor() as cur:
                        cur.execute(f"SAVEPOINT {q(savepoint)}")
                        try:
                            cur.execute(mixed_sql)
                            log.append(
                                f"semantic probe OK [{fk.name} MATCH SIMPLE 混合NULL被允许]"
                            )
                        except psycopg.Error as exc:
                            log.append(
                                f"semantic probe note [{fk.name} MATCH SIMPLE]: "
                                f"blocked by another constraint {exc.sqlstate}"
                            )
                        finally:
                            cur.execute(f"ROLLBACK TO SAVEPOINT {q(savepoint)}")
                            conn.commit()


def _snapshot(conn, schema: str, tables: dict[str, TableSchema], selected: list[str]):
    """Canonical, order-independent image of every loaded row.

    Row values (and NULLs) are serialised; ``ctid`` is deliberately excluded so
    a rolled-back probe can never masquerade as a data change.
    """
    snap: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        for tname in selected:
            cols = [c.name for c in tables[tname].columns]
            cur.execute(
                f"SELECT {', '.join(q(c) for c in cols)} FROM {q(schema)}.{q(tname)}"
            )
            snap[tname] = sorted(
                json.dumps(list(row), ensure_ascii=False, default=str)
                for row in cur.fetchall()
            )
    return snap


def _assert_partial_set_null_actions(
    conn, schema: str, tables: dict[str, TableSchema], selected: list[str], log: list[str]
) -> None:
    """Exercise exact ``ON DELETE SET NULL (col, ...)`` semantics.

    For a composite foreign key whose source constraint nulls only a subset of
    its columns, deleting a referenced row must:
      * keep the child row (it is not cascaded away),
      * NULL exactly the listed columns,
      * leave the other key columns untouched.
    All of this happens in a savepoint that is rolled back, so the verified
    sample is unchanged. Under MATCH FULL, a partial column list makes the
    child key mixed NULL/non-NULL, so PostgreSQL must reject the parent DELETE
    (23503); that rejection is itself the distinguishing signal against a
    dropped column list, which would null the whole key and succeed.

    When the referenced table is also targeted by other foreign keys, a parent
    DELETE is not attributable to this constraint alone, so the probe is
    skipped and the (already exact) catalog comparison remains authoritative.
    """
    # How many foreign keys across the whole closure target each parent table.
    referencing: dict[str, list[tuple[str, ForeignKey]]] = {}
    for t in selected:
        for f in tables[t].foreign_keys:
            referencing.setdefault(f.ref_table, []).append((t, f))

    for tname in selected:
        ts = tables[tname]
        for fk in ts.foreign_keys:
            if not _has_partial_set_null(fk):
                continue
            others = [x for x in referencing.get(fk.ref_table, []) if x[1] is not fk]
            if others:
                log.append(
                    f"skip partial SET NULL probe {fk.name}: parent table "
                    f"{fk.ref_table} is also referenced by "
                    f"{', '.join(f.name for _, f in others)}"
                )
                continue
            if fk.match.value == "MATCH FULL":
                # With MATCH FULL, nulling only part of the key makes the child
                # key mixed NULL/non-NULL, which PostgreSQL rejects (23503).
                # A whole-key SET NULL (dropped column list) would instead be
                # accepted, so this probe distinguishes the two actions.
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {', '.join(q(c) for c in fk.columns)} "
                        f"FROM {q(schema)}.{q(tname)} "
                        f"WHERE {' AND '.join(q(c) + ' IS NOT NULL' for c in fk.columns)} "
                        f"LIMIT 1"
                    )
                    child_key = cur.fetchone()
                if child_key is None:
                    log.append(
                        f"skip partial SET NULL probe {fk.name}: no fully non-NULL child key"
                    )
                    continue
                where_parent = " AND ".join(
                    f"{q(c)} = {_lit(v)}"
                    for c, v in zip(fk.ref_columns, child_key)
                )
                savepoint = f"sp_{secrets.token_hex(6)}"
                with conn.cursor() as cur:
                    cur.execute(f"SAVEPOINT {q(savepoint)}")
                    try:
                        cur.execute(
                            f"DELETE FROM {q(schema)}.{q(fk.ref_table)} "
                            f"WHERE {where_parent}"
                        )
                    except psycopg.Error as exc:
                        if exc.sqlstate != "23503":
                            raise VerificationError(
                                f"SET NULL 动作探测异常：{fk.name} MATCH FULL 部分置空"
                                f" 期望 23503，实际 {exc.sqlstate}: {exc}"
                            ) from exc
                        log.append(
                            f"semantic probe OK [{fk.name} MATCH FULL 部分置空列清单"
                            f" → 父行删除被 23503 拒绝]"
                        )
                    else:
                        raise VerificationError(
                            f"SET NULL 动作语义错误：{fk.name} MATCH FULL 仅置空"
                            f"部分列时父行删除本应被拒绝，但执行成功（列清单可能丢失）"
                        )
                    finally:
                        cur.execute(f"ROLLBACK TO SAVEPOINT {q(savepoint)}")
                        conn.commit()
                continue
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {q(schema)}.{q(tname)}")
                (n_rows,) = cur.fetchone()
                if n_rows == 0:
                    log.append(
                        f"skip partial SET NULL probe {fk.name}: {tname} has 0 rows"
                    )
                    continue
                cur.execute(
                    f"SELECT {', '.join(q(c) for c in fk.columns)} "
                    f"FROM {q(schema)}.{q(tname)} "
                    f"WHERE {' AND '.join(q(c) + ' IS NOT NULL' for c in fk.columns)} "
                    f"LIMIT 1"
                )
                child_key = cur.fetchone()
            if child_key is None:
                log.append(
                    f"skip partial SET NULL probe {fk.name}: no fully non-NULL child key"
                )
                continue

            ref_cols = fk.ref_columns
            # The child FK is honoured, so a matching parent row exists. Look it
            # up explicitly anyway so the probe targets that exact row.
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(q(c) for c in ref_cols)} "
                    f"FROM {q(schema)}.{q(fk.ref_table)} WHERE "
                    + " AND ".join(
                        f"{q(rc)} = {_lit(v)}" for rc, v in zip(ref_cols, child_key)
                    )
                    + " LIMIT 1"
                )
                parent_key = cur.fetchone()
            if parent_key is None:
                log.append(
                    f"skip partial SET NULL probe {fk.name}: no matching parent row"
                )
                continue

            set_cols = fk.delete_set_columns
            kept = [
                (c, v) for c, v in zip(fk.columns, child_key) if c not in set_cols
            ]
            where_parent = " AND ".join(
                f"{q(rc)} = {_lit(v)}" for rc, v in zip(ref_cols, parent_key)
            )
            kept_where = (
                " AND ".join(f"{q(c)} = {_lit(v)}" for c, v in kept)
                if kept
                else "TRUE"
            )

            savepoint = f"sp_{secrets.token_hex(6)}"
            with conn.cursor() as cur:
                cur.execute(f"SAVEPOINT {q(savepoint)}")
                try:
                    cur.execute(
                        f"DELETE FROM {q(schema)}.{q(fk.ref_table)} "
                        f"WHERE ctid IN (SELECT ctid FROM {q(schema)}.{q(fk.ref_table)} "
                        f"WHERE {where_parent} LIMIT 1)"
                    )
                    if cur.rowcount != 1:
                        raise VerificationError(
                            f"SET NULL 动作探测失败：{fk.name} 父行未被删除"
                        )
                    null_checks = " AND ".join(f"{q(c)} IS NULL" for c in set_cols)
                    cur.execute(
                        f"SELECT count(*) FROM {q(schema)}.{q(tname)} "
                        f"WHERE {kept_where} AND {null_checks}"
                    )
                    (ok_rows,) = cur.fetchone()
                    if ok_rows != 1:
                        raise VerificationError(
                            f"SET NULL 动作语义错误：{fk.name} 父行删除后，"
                            f"子行未保持为“仅 {', '.join(set_cols)} 置空、"
                            f"其余键列不变”的形态（匹配 {ok_rows} 行）"
                        )
                    # Prove nothing was cascaded away either.
                    cur.execute(
                        f"SELECT count(*) FROM {q(schema)}.{q(tname)}"
                    )
                    (remaining,) = cur.fetchone()
                    if remaining != n_rows:
                        raise VerificationError(
                            f"SET NULL 动作语义错误：{fk.name} 删除父行后子表行数"
                            f"由 {n_rows} 变为 {remaining}（应为保持）"
                        )
                    log.append(
                        f"semantic probe OK [{fk.name} ON DELETE SET NULL "
                        f"({', '.join(set_cols)}) 仅置空指定列且保留子行]"
                    )
                except psycopg.Error as exc:
                    # 23502 (NOT NULL) is exactly the bug this probe exists to
                    # catch: a whole-key SET NULL would try to null lab_id.
                    raise VerificationError(
                        f"SET NULL 动作探测失败：{fk.name} 删除父行时 "
                        f"{exc.sqlstate}: {exc}"
                    ) from exc
                finally:
                    cur.execute(f"ROLLBACK TO SAVEPOINT {q(savepoint)}")
                    conn.commit()


def _has_partial_set_null(fk: ForeignKey) -> bool:
    return (
        fk.on_delete == "SET NULL"
        and bool(fk.on_delete_set_columns)
        and len(fk.on_delete_set_columns) < len(fk.columns)
    )


def _lit(v) -> str:
    return "NULL" if v is None else str(v)
