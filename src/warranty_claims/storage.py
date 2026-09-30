"""保修责任与索赔管理的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN (
        'service_agent', 'warranty_admin', 'investigator', 'approver', 'auditor'
    )),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- 责任主体：原厂、第三方维修商、组件供应商、集成商、保险方或客户。
CREATE TABLE IF NOT EXISTS parties (
    party_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'oem', 'repairer', 'supplier', 'integrator', 'insurer', 'customer'
    )),
    contact TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

-- 储能电池整包；交付时间是装配保修的默认起算锚点。
CREATE TABLE IF NOT EXISTS packs (
    pack_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    owner_party_id TEXT NOT NULL REFERENCES parties(party_id),
    delivered_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 所有权转移历史；索赔受理时按故障发生时间回溯当时所有权人。
CREATE TABLE IF NOT EXISTS ownership_transfers (
    transfer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id TEXT NOT NULL REFERENCES packs(pack_id),
    from_owner_party_id TEXT,
    to_owner_party_id TEXT NOT NULL REFERENCES parties(party_id),
    transferred_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ownership_transfers_pack_time
ON ownership_transfers(pack_id, transferred_at);

-- 组件实例：原厂壳体、维修模组、更换的 BMS 等。
CREATE TABLE IF NOT EXISTS components (
    component_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('enclosure', 'module', 'bms', 'cell', 'other')),
    serial TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 组件在“装配 / 维修 / 更换”每个版本上的保修条款。
-- 维修动作是否改变责任期限由 term_effect 表达：
--   continue  沿用被取代版本的剩余期限（不延长）；
--   reset     维修件自维修日起重新起算的短保修；
--   new_full  自维修或更换日起的全新完整期限。
CREATE TABLE IF NOT EXISTS component_terms (
    component_id TEXT NOT NULL REFERENCES components(component_id),
    version INTEGER NOT NULL CHECK (version > 0),
    change_type TEXT NOT NULL CHECK (change_type IN ('assembly', 'repair', 'replacement')),
    warrantor_party_id TEXT NOT NULL REFERENCES parties(party_id),
    coverage_json TEXT NOT NULL,
    exclusions_json TEXT NOT NULL,
    start_condition TEXT NOT NULL,
    end_condition TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    duration_months INTEGER CHECK (duration_months IS NULL OR duration_months > 0),
    term_effect TEXT NOT NULL DEFAULT '' CHECK (term_effect IN ('', 'reset', 'continue', 'new_full')),
    supersedes_version INTEGER,
    predecessor_component_id TEXT REFERENCES components(component_id),
    recorded_by TEXT NOT NULL REFERENCES users(user_id),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (component_id, version),
    FOREIGN KEY (component_id, supersedes_version) REFERENCES component_terms(component_id, version)
);

-- 整包配置：每个槽位当前装入的组件版本；更换通过关闭旧行、插入新行留痕。
CREATE TABLE IF NOT EXISTS pack_slots (
    slot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id TEXT NOT NULL REFERENCES packs(pack_id),
    position TEXT NOT NULL,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    installed_version INTEGER NOT NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('assembly', 'replacement')),
    installed_at TEXT NOT NULL,
    removed_at TEXT,
    installed_by TEXT NOT NULL REFERENCES users(user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_slot_per_component
ON pack_slots(component_id) WHERE removed_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS one_active_slot_per_position
ON pack_slots(pack_id, position) WHERE removed_at IS NULL;

-- 索赔单：同一故障同时只能有一单在途；已赔付故障由 paid_faults 单独兜底。
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    pack_id TEXT NOT NULL REFERENCES packs(pack_id),
    customer_party_id TEXT NOT NULL REFERENCES parties(party_id),
    fault_key TEXT NOT NULL,
    symptom TEXT NOT NULL,
    failure_at TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'investigating', 'awaiting_evidence', 'allocating',
        'settlement_pending', 'rejection_pending',
        'settled', 'rejected', 'reopened'
    )),
    current_revision INTEGER NOT NULL DEFAULT 1 CHECK (current_revision > 0),
    frozen_at TEXT,
    freeze_sha256 TEXT,
    notified_conclusion_at TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_claim_per_fault
ON claims(fault_key)
WHERE state IN (
    'investigating', 'awaiting_evidence', 'allocating',
    'settlement_pending', 'rejection_pending', 'reopened'
);

-- 受理时形成的不可变快照：当时配置、所有权、有效条款、初始证据。
CREATE TABLE IF NOT EXISTS claim_freeze (
    claim_id TEXT PRIMARY KEY REFERENCES claims(claim_id),
    frozen_at TEXT NOT NULL,
    failure_at TEXT NOT NULL,
    owner_party_id TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64)
);

-- 故障证据只能追加，补证与迟到检测都产生新行。
CREATE TABLE IF NOT EXISTS claim_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    source TEXT NOT NULL CHECK (source IN ('intake', 'supplement', 'late')),
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    submitted_at TEXT NOT NULL,
    UNIQUE (claim_id, content_sha256)
);

-- 每个在保责任方在受理时即获得独立证据保全；不得因其他方确认而提前释放。
CREATE TABLE IF NOT EXISTS evidence_holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    status TEXT NOT NULL DEFAULT 'held' CHECK (status IN ('held', 'released')),
    held_reason TEXT NOT NULL,
    released_by TEXT REFERENCES users(user_id),
    released_at TEXT,
    release_note TEXT,
    UNIQUE (claim_id, party_id)
);

-- 有版本的决定：调查、补证要求、责任分摊、和解、驳回、复开各自占一个修订号。
CREATE TABLE IF NOT EXISTS claim_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    kind TEXT NOT NULL CHECK (kind IN (
        'intake', 'investigation', 'supplement_request', 'allocation',
        'settlement', 'rejection', 'reopen'
    )),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'notified')),
    content_json TEXT NOT NULL,
    basis_sha256 TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    notified_at TEXT,
    notification_ref TEXT,
    UNIQUE (claim_id, revision)
);

-- 责任分摊明细挂在具体分摊修订上；确认与争议只作用于最新修订。
CREATE TABLE IF NOT EXISTS claim_allocations (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    revision INTEGER NOT NULL,
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    share_basis_points INTEGER NOT NULL CHECK (share_basis_points BETWEEN 0 AND 10000),
    amount_cny TEXT NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'confirmed', 'disputed')),
    confirmation_ref TEXT,
    confirmed_at TEXT,
    UNIQUE (claim_id, revision, party_id)
);

CREATE TABLE IF NOT EXISTS claim_disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    subject TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    opened_by TEXT NOT NULL REFERENCES users(user_id),
    opened_at TEXT NOT NULL,
    resolved_by TEXT REFERENCES users(user_id),
    resolved_at TEXT,
    resolution TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_dispute_per_subject
ON claim_disputes(claim_id, party_id, subject) WHERE status = 'open';

-- 结论通知客户之后才到达的检测结果：先挂起登记，只能经复开进入流程。
CREATE TABLE IF NOT EXISTS late_findings (
    finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    label TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    summary TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    registered_at TEXT NOT NULL,
    carried_into_revision INTEGER
);

-- 赔付台账与故障唯一赔付索引：不同索赔不得就同一故障再次赔付。
CREATE TABLE IF NOT EXISTS claim_payments (
    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    fault_key TEXT NOT NULL,
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    decision_revision INTEGER NOT NULL,
    amount_cny TEXT NOT NULL,
    paid_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paid_faults (
    fault_key TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    settled_revision INTEGER NOT NULL,
    paid_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 已通知客户的决定不可改写：数据库层兜底，任何 UPDATE/DELETE 都被拒绝。
CREATE TRIGGER IF NOT EXISTS claim_decisions_notified_no_update
BEFORE UPDATE ON claim_decisions
WHEN OLD.notified_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'notified decision is immutable');
END;

CREATE TRIGGER IF NOT EXISTS claim_decisions_notified_no_delete
BEFORE DELETE ON claim_decisions
WHEN OLD.notified_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'notified decision is immutable');
END;

-- 受理快照自形成起不可变。
CREATE TRIGGER IF NOT EXISTS claim_freeze_no_update
BEFORE UPDATE ON claim_freeze
BEGIN
    SELECT RAISE(ABORT, 'claim freeze snapshot is immutable');
END;

CREATE TRIGGER IF NOT EXISTS claim_freeze_no_delete
BEFORE DELETE ON claim_freeze
BEGIN
    SELECT RAISE(ABORT, 'claim freeze snapshot is immutable');
END;
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "parties", "packs", "ownership_transfers",
    "components", "component_terms", "pack_slots", "claims", "claim_freeze",
    "claim_evidence", "evidence_holds", "claim_decisions", "claim_allocations",
    "claim_disputes", "late_findings", "claim_payments", "paid_faults",
    "audit_events",
})

TERMINAL_STATES = frozenset({"settled", "rejected"})
IN_FLIGHT_STATES = frozenset({
    "investigating", "awaiting_evidence", "allocating",
    "settlement_pending", "rejection_pending", "reopened",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None)
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
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
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
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
