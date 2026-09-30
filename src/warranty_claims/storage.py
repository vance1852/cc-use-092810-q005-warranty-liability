"""保修责任与索赔服务的 SQLite 模式与事务辅助。

不可变原则体现在结构上：
- 条款、配置、决定全部只追加（版本列），没有更新接口；
- 索赔结论一旦通知客户（notified_at 非空），其 decision_id 即被冻结；
- 已赔付故障指纹在 fault_payouts 中永久唯一；
- 证据保存在所有责任方终局前只能保持 held（放行必须留下 evidence_release 决定）。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS wc_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wc_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN
        ('service_agent', 'warranty_manager', 'claims_adjuster', 'finance', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- 责任方目录：原厂壳体厂、第三方维修商、BMS 供应商等。
CREATE TABLE IF NOT EXISTS warrantors (
    party_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    party_type TEXT NOT NULL CHECK (party_type IN
        ('oem', 'repair_vendor', 'component_supplier', 'insurer', 'internal', 'other')),
    contact TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

-- 组件实例登记：序列号 -> 组件类别与来源责任方。
CREATE TABLE IF NOT EXISTS components (
    component_serial TEXT PRIMARY KEY,
    component_kind TEXT NOT NULL,
    source_party_id TEXT REFERENCES warrantors(party_id),
    created_at TEXT NOT NULL
);

-- 带版本的保修条款：覆盖范围、起止条件、排除条款。
CREATE TABLE IF NOT EXISTS warranty_terms (
    terms_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    component_kind TEXT NOT NULL,
    warrantor_id TEXT NOT NULL REFERENCES warrantors(party_id),
    title TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    exclusions_json TEXT NOT NULL,
    start_basis TEXT NOT NULL,
    start_condition TEXT NOT NULL,
    end_basis TEXT NOT NULL,
    end_condition TEXT NOT NULL,
    liability_limit_cny TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES wc_users(user_id),
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    PRIMARY KEY (terms_id, version)
);

-- 整包所有权链，受理时冻结当时所有权人。
CREATE TABLE IF NOT EXISTS pack_ownership (
    ownership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_serial TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    recorded_by TEXT NOT NULL REFERENCES wc_users(user_id),
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pack_ownership ON pack_ownership(pack_serial, valid_from);

-- 装配/维修配置版本。
CREATE TABLE IF NOT EXISTS configurations (
    config_id TEXT PRIMARY KEY,
    pack_serial TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN
        ('assembly', 'repair', 'inspection', 'decommission')),
    event_at TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    work_order_ref TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    recorded_by TEXT NOT NULL REFERENCES wc_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (pack_serial, sequence_no)
);

-- 配置槽位：组件实例在该版本上挂载的条款版本被逐字冻结。
CREATE TABLE IF NOT EXISTS config_slots (
    config_id TEXT NOT NULL REFERENCES configurations(config_id),
    slot_id TEXT NOT NULL,
    component_serial TEXT NOT NULL,
    component_kind TEXT NOT NULL,
    terms_id TEXT NOT NULL,
    terms_version INTEGER NOT NULL,
    fitted_at TEXT NOT NULL,
    action TEXT NOT NULL,
    PRIMARY KEY (config_id, slot_id),
    UNIQUE (config_id, component_serial),
    FOREIGN KEY (terms_id, terms_version) REFERENCES warranty_terms(terms_id, version)
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    pack_serial TEXT NOT NULL,
    failure_occurred_at TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    reported_by_owner_id TEXT NOT NULL,
    fault_fingerprint TEXT NOT NULL,
    -- 冻结三要素：配置版本、所有权、有效条款在受理事务内复制。
    frozen_config_id TEXT NOT NULL REFERENCES configurations(config_id),
    frozen_owner_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('open', 'investigating', 'allocation', 'settled', 'rejected', 'reopened', 'closed')),
    current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
    -- 最近一次已通知客户的终局决定；迟到检测只能复开，不能改写它。
    notified_decision_id INTEGER,
    -- 历次已通知结论链（最近一次之前的那一个），旧结论行本身永不删改。
    prior_notified_decision_id INTEGER,
    notified_at TEXT,
    summary TEXT NOT NULL,
    UNIQUE (pack_serial, fault_fingerprint)
);

-- 受理时冻结的槽位/条款副本（即使后续条款修订或组件再次维修也不变）。
CREATE TABLE IF NOT EXISTS claim_frozen_slots (
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    slot_id TEXT NOT NULL,
    component_serial TEXT NOT NULL,
    component_kind TEXT NOT NULL,
    fitted_at TEXT NOT NULL,
    claimed INTEGER NOT NULL DEFAULT 0 CHECK (claimed IN (0, 1)),
    symptom TEXT NOT NULL DEFAULT '',
    terms_id TEXT NOT NULL,
    terms_version INTEGER NOT NULL,
    warrantor_id TEXT NOT NULL,
    title TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    exclusions_json TEXT NOT NULL,
    start_basis TEXT NOT NULL,
    start_condition TEXT NOT NULL,
    end_basis TEXT NOT NULL,
    end_condition TEXT NOT NULL,
    liability_limit_cny TEXT NOT NULL,
    terms_sha256 TEXT NOT NULL,
    -- 受理时按故障时点对条款起止条件的机器判定快照。
    effective_start TEXT,
    effective_end TEXT,
    effectiveness_state TEXT NOT NULL,
    effectiveness_note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (claim_id, slot_id)
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    evidence_ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    captured_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    frozen_at TEXT NOT NULL,
    UNIQUE (claim_id, evidence_ref)
);

-- 每个潜在责任方一行证据保全；放行后如因复开需重新持有则新增行，旧行保留。
CREATE TABLE IF NOT EXISTS evidence_holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    party_id TEXT NOT NULL REFERENCES warrantors(party_id),
    status TEXT NOT NULL CHECK (status IN ('held', 'released')),
    -- held 期间追加保全的证据指纹（补证也进保全）。
    scope TEXT NOT NULL DEFAULT 'all' CHECK (scope IN ('all', 'partial')),
    held_at TEXT NOT NULL,
    released_at TEXT,
    release_decision_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_evidence_holds_party
ON evidence_holds(claim_id, party_id, hold_id);

-- 已通知客户的决定（只追加）。
CREATE TABLE IF NOT EXISTS claim_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    kind TEXT NOT NULL CHECK (kind IN
        ('investigation', 'evidence_supplement', 'liability_allocation',
         'settlement', 'rejection', 'reopen', 'evidence_release', 'affirmation')),
    status TEXT NOT NULL,
    basis TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    supersedes_revision INTEGER,
    decided_by TEXT NOT NULL REFERENCES wc_users(user_id),
    decided_at TEXT NOT NULL,
    notified_at TEXT,
    notified_to TEXT,
    UNIQUE (claim_id, revision)
);

-- 同一故障指纹只允许一次赔付结案。
CREATE TABLE IF NOT EXISTS fault_payouts (
    fault_fingerprint TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    decision_id INTEGER NOT NULL REFERENCES claim_decisions(decision_id),
    paid_at TEXT NOT NULL
);

-- 和解/维修更换后剩余保修的延续登记，挂在原组件与替换组件之间。
CREATE TABLE IF NOT EXISTS warranty_continuations (
    continuation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    decision_id INTEGER NOT NULL REFERENCES claim_decisions(decision_id),
    original_serial TEXT NOT NULL,
    replacement_serial TEXT,
    warrantor_id TEXT NOT NULL REFERENCES warrantors(party_id),
    terms_id TEXT NOT NULL,
    terms_version INTEGER NOT NULL,
    continuation_rule TEXT NOT NULL
        CHECK (continuation_rule IN ('remaining', 'reset', 'extended', 'excluded')),
    remaining_end_condition TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wc_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wc_audit_entity
ON wc_audit_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "wc_schema_meta", "wc_users", "warrantors", "components", "warranty_terms",
    "pack_ownership", "configurations", "config_slots", "claims", "claim_frozen_slots",
    "claim_evidence", "evidence_holds", "claim_decisions", "fault_payouts",
    "warranty_continuations", "wc_audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用外键与忙等待。

    ThreadingHTTPServer 会在工作线程中复用同一连接，因此关闭同线程限制；
    所有写操作都走 BEGIN IMMEDIATE，配合 busy_timeout 完成事务串行化。
    """

    connection = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化全部表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO wc_schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM wc_schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
