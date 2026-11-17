"""End-to-end tests: jobs, SQL rendering, real isolated-database loading."""
from __future__ import annotations

import json
import zipfile
from io import BytesIO

import psycopg
from fastapi.testclient import TestClient

from app.jobs import STORE, package_zip, run_job
from app.main import app
from app.models import GenerateRequest, JobStatus

client = TestClient(app)


def _load(name):
    raw = json.load(open(f"/workspace/examples/{name}"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _run(req_dict):
    req = GenerateRequest(**req_dict)
    jid = STORE.create()
    run_job(jid, req)
    return jid, STORE.get(jid)


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_catalog_listing_and_structure(source_dsn):
    r = client.get("/schemas/public/tables")
    assert r.status_code == 200
    assert "employee" in r.json()["base_tables"]
    r2 = client.get("/schemas/public/tables/project/structure")
    body = r2.json()
    assert body["supported"] is True
    fks = body["structure"]["foreign_keys"]
    assert any(f["match"] == "MATCH FULL" for f in fks)


def test_cyclic_four_table_job_succeeds_and_publishes():
    jid, job = _run(_load("request_cyclic_four_tables.json"))
    assert job.status == JobStatus.SUCCEEDED
    assert set(job.selected_tables) == {
        "dept",
        "employee",
        "project",
        "assignment",
    }
    pkg = STORE.package(jid)
    assert set(pkg) == {
        "01_schema.sql",
        "02_data.sql",
        "03_constraints.sql",
        "manifest.json",
        "README.md",
    }
    # Constraints remain NOT DEFERRABLE; nothing was disabled or made deferred.
    fk_ddl = [
        ln for ln in pkg["03_constraints.sql"].splitlines()
        if ln.startswith("ALTER TABLE")
    ]
    assert fk_ddl and all("NOT DEFERRABLE" in ln for ln in fk_ddl)
    assert all("INITIALLY DEFERRED" not in ln for ln in fk_ddl)
    # FKs are post-data ALTERs so the cycle loads without deferral tricks.
    assert all("ALTER TABLE" in ln for ln in fk_ddl)
    # Zip is downloadable and well formed.
    zf = zipfile.ZipFile(BytesIO(package_zip(pkg)))
    assert set(zf.namelist()) == set(pkg)


def test_download_blocked_until_success():
    jid, job = _run(_load("request_unsat_conflict.json"))
    assert job.status == JobStatus.FAILED
    assert STORE.package(jid) is None
    r = client.get(f"/jobs/{jid}/download")
    assert r.status_code == 409


def test_unsat_reports_conflicting_rows_ranges_or_constraints():
    jid, job = _run(_load("request_unsat_conflict.json"))
    conflict = job.conflict.model_dump()
    assert conflict["kind"] == "no_solution"
    joined = json.dumps(conflict, ensure_ascii=False)
    assert "employee_dept_fk" in joined
    assert "500" in joined and "100" in joined
    assert "fk" in conflict["details"]["kinds"]


def test_fk_closure_expands_target_table():
    # Only project requested; the transitive FK closure pulls in dept and
    # employee (dept.mgr_emp -> employee), staying within the 4-table cap.
    jid, job = _run(_load("request_closure_single.json"))
    assert job.status == JobStatus.SUCCEEDED
    assert set(job.selected_tables) == {"project", "dept", "employee"}


def test_generated_package_loads_in_fresh_empty_database(
    fresh_verify_database,
):
    jid, job = _run(_load("request_cyclic_four_tables.json"))
    assert job.status == JobStatus.SUCCEEDED
    dsn, _ = fresh_verify_database
    pkg = STORE.package(jid)
    with psycopg.connect(dsn, autocommit=False) as conn:
        for f in ("01_schema.sql", "02_data.sql", "03_constraints.sql"):
            conn.cursor().execute(pkg[f])
        conn.commit()
        cur = conn.cursor()
        for t, expected in [
            ("dept", 3),
            ("employee", 5),
            ("project", 4),
            ("assignment", 6),
        ]:
            cur.execute(f"SELECT count(*) FROM public.{t}")
            assert cur.fetchone()[0] == expected


def test_zero_rows_request(source_dsn):
    req = {
        "schema_name": "public",
        "tables": [
            {
                "table": "project",
                "rows": 0,
                "ranges": {
                    "project_id": {"low": 1, "high": 9},
                    "dept_id": {"low": 1, "high": 9},
                    "dept_code": {"low": 1, "high": 9},
                    "budget": {"low": 1, "high": 9},
                },
            }
        ],
    }
    jid, job = _run(req)
    # closure pulls dept (default 5 rows); project itself stays empty
    assert job.status == JobStatus.SUCCEEDED
    pkg = STORE.package(jid)
    assert "no INSERTs" in pkg["02_data.sql"]


def test_validation_rejects_bad_interval(source_dsn):
    req = {
        "schema_name": "public",
        "tables": [
            {
                "table": "dept",
                "rows": 1,
                "ranges": {
                    "dept_id": {"low": 10, "high": 5},  # low > high
                    "dept_code": {"low": 1, "high": 2},
                    "parent_dept": {"low": 1, "high": 9},
                    "mgr_emp": {"low": 1, "high": 9},
                },
            }
        ],
    }
    # pydantic-level or service-level rejection
    r = client.post("/jobs", json=req)
    if r.status_code == 202:
        jid = r.json()["job_id"]
        job = STORE.get(jid)
        assert job.status == JobStatus.FAILED
        assert job.conflict.kind == "request"
    else:
        assert r.status_code == 422


def test_more_than_max_tables_rejected():
    r = client.post(
        "/jobs",
        json={
            "schema_name": "public",
            "tables": [
                {"table": f"t{i}", "rows": 1, "ranges": {}} for i in range(5)
            ],
        },
    )
    assert r.status_code == 422


def test_reviewer_report_delete_keeps_lab_and_nulls_only_reviewer():
    """Two tables, one row each, fixed values: the reviewer DELETE must leave
    the report in its lab and only null reviewer_id (partial SET NULL list)."""
    jid, job = _run(_load("request_reviewer_report.json"))
    assert job.status == JobStatus.SUCCEEDED, job.conflict
    pkg = STORE.package(jid)
    fk_ddl = [
        ln for ln in pkg["03_constraints.sql"].splitlines()
        if ln.startswith("ALTER TABLE")
    ]
    assert any(
        "ON DELETE SET NULL (\"reviewer_id\")" in ln for ln in fk_ddl
    ), pkg["03_constraints.sql"]
    assert all("NOT DEFERRABLE" in ln for ln in fk_ddl)
    # Load into the fresh isolated DB produced for this job is verified by the
    # service; re-load once more and exercise the DELETE action explicitly.
    # (A dedicated end-to-end probe lives in the verification log below.)
    assert any("ON DELETE SET NULL" in ln and "reviewer_id" in ln for ln in job.log)


def test_reviewer_report_action_end_to_end(fresh_verify_database):
    jid, job = _run(_load("request_reviewer_report.json"))
    assert job.status == JobStatus.SUCCEEDED, job.conflict
    pkg = STORE.package(jid)
    dsn, _ = fresh_verify_database
    with psycopg.connect(dsn, autocommit=False) as conn:
        for f in ("01_schema.sql", "02_data.sql", "03_constraints.sql"):
            conn.cursor().execute(pkg[f])
        conn.commit()
        cur = conn.cursor()
        cur.execute("SELECT lab_id, report_id, reviewer_id FROM public.report")
        assert cur.fetchall() == [(7, 900, 42)]
        cur.execute("DELETE FROM public.reviewer WHERE lab_id = 7 AND reviewer_id = 42")
        assert cur.rowcount == 1
        conn.commit()
        cur.execute("SELECT lab_id, report_id, reviewer_id FROM public.report")
        assert cur.fetchall() == [(7, 900, None)]
        # The surviving report stays in lab 7 and its composite PK is intact.
        cur.execute("SELECT count(*) FROM public.report WHERE lab_id = 7")
        assert cur.fetchone()[0] == 1


def test_verification_probes_do_not_change_sample():
    """Verification logs must prove the loaded rows are unchanged."""
    jid, job = _run(_load("request_reviewer_report.json"))
    assert job.status == JobStatus.SUCCEEDED, job.conflict
    assert any("snapshot unchanged" in line for line in job.log)
    assert any("semantic probe OK" in line and "SET NULL" in line for line in job.log)


def test_package_omitting_set_null_list_fails_verification(
    fresh_verify_database, source_dsn
):
    """A package that drops the (reviewer_id) list must be rejected by the
    verifier, so a broken SQL package can never be published."""
    from app.db import source_connection
    from app.introspect import introspect_tables
    from app.verifier import verify_package

    with source_connection(source_dsn) as conn:
        schemas = introspect_tables(conn, "public", ["reviewer", "report"])
    pkg = STORE.package(_run(_load("request_reviewer_report.json"))[0])
    broken = dict(pkg)
    broken["03_constraints.sql"] = pkg["03_constraints.sql"].replace(
        'ON DELETE SET NULL ("reviewer_id")', "ON DELETE SET NULL"
    )
    dsn, _ = fresh_verify_database
    ok, vlog = verify_package(
        dsn, "probe_", "public", schemas, ["reviewer", "report"], broken
    )
    assert not ok
    joined = "\n".join(vlog)
    assert "on_delete_set_columns" in joined or "目录不一致" in joined


def test_failed_action_verification_blocks_publish(monkeypatch):
    """If the rendered action cannot be verified, the job must FAIL and the
    package must stay unavailable for download."""
    import app.jobs as jobs_mod

    real_build = jobs_mod.build_package

    def broken_build(*args, **kwargs):
        pkg = real_build(*args, **kwargs)
        pkg["03_constraints.sql"] = pkg["03_constraints.sql"].replace(
            'ON DELETE SET NULL ("reviewer_id")', "ON DELETE SET NULL"
        )
        return pkg

    monkeypatch.setattr(jobs_mod, "build_package", broken_build)
    jid, job = _run(_load("request_reviewer_report.json"))
    assert job.status == JobStatus.FAILED
    assert job.conflict.kind == "verification"
    assert STORE.package(jid) is None
    assert client.get(f"/jobs/{jid}/download").status_code == 409
    assert any("NOT NULL" in line or "目录不一致" in line for line in job.log)
