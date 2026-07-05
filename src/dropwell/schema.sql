CREATE TABLE IF NOT EXISTS "drop" (
  id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  body TEXT NOT NULL,
  received_at TEXT NOT NULL,
  updated_at TEXT,
  status TEXT NOT NULL DEFAULT 'inbound'
);
CREATE INDEX IF NOT EXISTS idx_drop_topic ON "drop"(topic);
CREATE INDEX IF NOT EXISTS idx_drop_received_at ON "drop"(received_at);
CREATE INDEX IF NOT EXISTS idx_drop_status ON "drop"(status);
