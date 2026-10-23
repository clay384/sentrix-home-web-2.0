ALTER TABLE assets ADD COLUMN external_source_id TEXT;
ALTER TABLE assets ADD COLUMN source_timestamp TEXT;
ALTER TABLE assets ADD COLUMN source_location TEXT NOT NULL DEFAULT '';
ALTER TABLE assets ADD COLUMN source_metadata_json TEXT NOT NULL DEFAULT '{}';
CREATE UNIQUE INDEX idx_assets_external_source_id ON assets(scope_id, external_source_id)
WHERE external_source_id IS NOT NULL;
