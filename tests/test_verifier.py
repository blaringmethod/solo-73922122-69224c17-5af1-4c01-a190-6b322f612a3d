"""Verifier-level tests for exact ON DELETE SET NULL column-list semantics."""
from __future__ import annotations

from app.models import (
    Column,
    ForeignKey,
    IntRange,
    MatchType,
    TableSchema,
    TableSpec,
    UniqueConstraint,
)
from app.solver import Generator
from app.sqlgen import build_package
from app.verifier import verify_package


def _col(name, nullable=False):
    return Column(
        name=name,
        pg_type="integer",
        ordinal=1,
        nullable=nullable,
        has_default=False,
        is_identity=False,
        is_generated=False,
    )


def _schemas(match: MatchType):
    parent = TableSchema(
        schema_name="public",
        name="p",
        columns=[_col("a"), _col("b")],
        unique_constraints=[
            UniqueConstraint(name="p_pk", columns=["a", "b"], is_primary=True)
        ],
        foreign_keys=[],
    )
    child = TableSchema(
        schema_name="public",
        name="c",
        columns=[_col("id"), _col("a", nullable=True), _col("b", nullable=True)],
        unique_constraints=[
            UniqueConstraint(name="c_pk", columns=["id"], is_primary=True)
        ],
        foreign_keys=[
            ForeignKey(
                name="c_fk",
                columns=["a", "b"],
                ref_table="p",
                ref_columns=["a", "b"],
                match=match,
                on_update="NO ACTION",
                on_delete="SET NULL",
                on_delete_set_columns=["b"],
                not_deferrable=True,
                initially_deferred=False,
            )
        ],
    )
    specs = {
        "p": TableSpec(
            table="p",
            rows=1,
            ranges={"a": IntRange(low=1, high=1), "b": IntRange(low=2, high=2)},
        ),
        "c": TableSpec(
            table="c",
            rows=1,
            ranges={
                "id": IntRange(low=10, high=10),
                "a": IntRange(low=1, high=1),
                "b": IntRange(low=2, high=2),
            },
        ),
    }
    return {"p": parent, "c": child}, specs


def test_partial_set_null_simple_passes_and_verifies(fresh_verify_database):
    tables, specs = _schemas(MatchType.SIMPLE)
    data = Generator(tables, specs).solve()
    pkg = build_package(tables, ["p", "c"], specs, data)
    dsn, _ = fresh_verify_database
    ok, log = verify_package(dsn, "vsimple_", "public", tables, ["p", "c"], pkg)
    assert ok, log
    joined = "\n".join(log)
    assert "ON DELETE SET NULL (\"b\")" in pkg["03_constraints.sql"]
    assert "仅置空指定列且保留子行" in joined
    assert "snapshot unchanged" in joined


def test_partial_set_null_dropped_list_is_rejected(fresh_verify_database):
    tables, specs = _schemas(MatchType.SIMPLE)
    data = Generator(tables, specs).solve()
    pkg = build_package(tables, ["p", "c"], specs, data)
    broken = dict(pkg)
    broken["03_constraints.sql"] = pkg["03_constraints.sql"].replace(
        'ON DELETE SET NULL ("b")', "ON DELETE SET NULL"
    )
    dsn, _ = fresh_verify_database
    ok, log = verify_package(dsn, "vbroken_", "public", tables, ["p", "c"], broken)
    assert not ok
    assert any("目录不一致" in line for line in log), log


def test_match_full_partial_set_null_delete_is_rejected_by_pg(
    fresh_verify_database,
):
    """Under MATCH FULL the partial list makes the parent DELETE illegal; the
    verifier asserts that rejection, which a dropped list would not produce."""
    tables, specs = _schemas(MatchType.FULL)
    data = Generator(tables, specs).solve()
    pkg = build_package(tables, ["p", "c"], specs, data)
    dsn, _ = fresh_verify_database
    ok, log = verify_package(dsn, "vfull_", "public", tables, ["p", "c"], pkg)
    assert ok, log
    assert any("MATCH FULL" in line and "23503" in line for line in log), log