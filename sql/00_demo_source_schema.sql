-- ============================================================
-- Demo source schema: cyclic referential graph used for the
-- circular-reference worked example in /examples, plus the
-- reviewer/report pair used for the partial SET NULL regression.
--
-- All columns are integer; no patient/real data is included.
--
-- Graph:
--   dept  (mgr_emp)  ──► employee.emp_id        (cycle 1)
--   employee.dept_id ──► dept.dept_id           (cycle 1)
--   employee.mentor_id ─► employee.emp_id       (self reference)
--   project (dept_id, dept_code) MATCH FULL ─► dept (dept_id, dept_code)
--   assignment (emp_id, project_id) ─► employee / project, composite UK
--   report (lab_id, reviewer_id) ─► reviewer (lab_id, reviewer_id)
--       ON DELETE SET NULL (reviewer_id)   -- partial column list
-- ============================================================

DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;

CREATE TABLE dept (
    dept_id    integer NOT NULL,
    dept_code  smallint NOT NULL,
    parent_dept integer,                       -- nullable, self reference below
    mgr_emp    integer,                        -- nullable cycle FK -> employee
    CONSTRAINT dept_pk PRIMARY KEY (dept_id),
    CONSTRAINT dept_code_uk UNIQUE (dept_id, dept_code),
    CONSTRAINT dept_parent_fk FOREIGN KEY (parent_dept)
        REFERENCES dept (dept_id) MATCH SIMPLE
        ON DELETE SET NULL ON UPDATE NO ACTION NOT DEFERRABLE
    -- dept_mgr_fk is added after employee exists (circular reference)
);

CREATE TABLE employee (
    emp_id     integer NOT NULL,
    dept_id    integer NOT NULL,
    mentor_id  integer,                       -- nullable self FK
    level      smallint NOT NULL,
    CONSTRAINT employee_pk PRIMARY KEY (emp_id),
    CONSTRAINT employee_dept_fk FOREIGN KEY (dept_id)
        REFERENCES dept (dept_id) MATCH SIMPLE
        ON DELETE RESTRICT ON UPDATE CASCADE NOT DEFERRABLE,
    CONSTRAINT employee_mentor_fk FOREIGN KEY (mentor_id)
        REFERENCES employee (emp_id) MATCH SIMPLE
        ON DELETE SET NULL ON UPDATE NO ACTION NOT DEFERRABLE
);

CREATE TABLE project (
    project_id bigint NOT NULL,
    dept_id    integer,                       -- part of nullable MATCH FULL key
    dept_code  smallint,
    budget     integer,
    CONSTRAINT project_pk PRIMARY KEY (project_id),
    CONSTRAINT project_dept_fk FOREIGN KEY (dept_id, dept_code)
        REFERENCES dept (dept_id, dept_code) MATCH FULL
        ON DELETE NO ACTION ON UPDATE NO ACTION NOT DEFERRABLE
);

CREATE TABLE assignment (
    emp_id      integer NOT NULL,
    project_id  bigint NOT NULL,
    role_code   integer,                      -- nullable plain column
    CONSTRAINT assignment_pk PRIMARY KEY (emp_id, project_id),
    CONSTRAINT assignment_emp_fk FOREIGN KEY (emp_id)
        REFERENCES employee (emp_id) MATCH SIMPLE
        ON DELETE CASCADE ON UPDATE CASCADE NOT DEFERRABLE,
    CONSTRAINT assignment_project_fk FOREIGN KEY (project_id)
        REFERENCES project (project_id) MATCH SIMPLE
        ON DELETE CASCADE ON UPDATE CASCADE NOT DEFERRABLE
);

-- Close the circular reference dept -> employee -> dept.
ALTER TABLE dept
    ADD CONSTRAINT dept_mgr_fk FOREIGN KEY (mgr_emp)
        REFERENCES employee (emp_id) MATCH SIMPLE
        ON DELETE SET NULL ON UPDATE CASCADE NOT DEFERRABLE;

-- ------------------------------------------------------------------
-- Reviewer / report: (lab_id, reviewer_id) links a report to its
-- reviewer. lab_id is NOT NULL and is also part of the report primary
-- key, so a DELETE of the reviewer may only null reviewer_id -- never
-- lab_id. The source constraint therefore uses the partial column list
--   ON DELETE SET NULL (reviewer_id)
-- (PostgreSQL 15+). A generated package that omits the column list
-- would null the whole key and be rejected by report_pk / NOT NULL.
-- ------------------------------------------------------------------
CREATE TABLE reviewer (
    lab_id       integer NOT NULL,
    reviewer_id  integer NOT NULL,
    CONSTRAINT reviewer_pk PRIMARY KEY (lab_id, reviewer_id)
);

CREATE TABLE report (
    lab_id       integer NOT NULL,
    report_id    integer NOT NULL,
    reviewer_id  integer,                      -- nullable: the only SET NULL target
    CONSTRAINT report_pk PRIMARY KEY (lab_id, report_id),
    CONSTRAINT report_reviewer_fk
        FOREIGN KEY (lab_id, reviewer_id)
        REFERENCES reviewer (lab_id, reviewer_id) MATCH SIMPLE
        ON UPDATE NO ACTION ON DELETE SET NULL (reviewer_id)
        NOT DEFERRABLE
);
