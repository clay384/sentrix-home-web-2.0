-- 记忆图是从 2.0 标准记忆表重建出来的派生投影。
-- 不给节点/边加跨表外键：源事件、观察、实体发生合并或重建时，图按 scope 原子替换，
-- 旧图不能反过来阻挡权威记忆表的写入。
CREATE TABLE memory_graph_nodes (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    build_revision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_memory_graph_nodes_scope_type
    ON memory_graph_nodes(scope_id, node_type);

CREATE TABLE memory_graph_edges (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    build_revision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_memory_graph_edges_scope_source
    ON memory_graph_edges(scope_id, source_id);
CREATE INDEX idx_memory_graph_edges_scope_target
    ON memory_graph_edges(scope_id, target_id);
CREATE INDEX idx_memory_graph_edges_scope_relation
    ON memory_graph_edges(scope_id, relation);

CREATE TABLE memory_graph_builds (
    scope_id TEXT PRIMARY KEY,
    build_revision TEXT NOT NULL,
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    built_at TEXT NOT NULL
);
