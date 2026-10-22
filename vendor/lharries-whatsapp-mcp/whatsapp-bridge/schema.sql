-- WhatsApp data the bridge keeps in messages.db, next to the whatsmeow session
-- store (whatsapp.db). The bridge creates and writes it; the whatsapp CLI and
-- MCP backend read it. Times are epoch milliseconds; optional text is NULL
-- when absent; media keys and hashes are raw bytes.
CREATE TABLE IF NOT EXISTS chats (
	jid TEXT PRIMARY KEY,
	name TEXT,
	last_message_time INTEGER
);
CREATE INDEX IF NOT EXISTS chats_by_last ON chats (last_message_time);

CREATE TABLE IF NOT EXISTS messages (
	id TEXT NOT NULL,
	chat_jid TEXT NOT NULL,
	sender TEXT NOT NULL DEFAULT '',
	content TEXT NOT NULL DEFAULT '',
	timestamp INTEGER NOT NULL,
	is_from_me INTEGER NOT NULL DEFAULT 0,
	media_type TEXT,
	reply_to_message_id TEXT,
	reply_to_sender TEXT,
	reply_to_content TEXT,
	reply_to_media_type TEXT,
	filename TEXT,
	url TEXT,
	media_key BLOB,
	file_sha256 BLOB,
	file_enc_sha256 BLOB,
	file_length INTEGER,
	edited_at INTEGER,
	PRIMARY KEY (chat_jid, id)
);
CREATE INDEX IF NOT EXISTS messages_by_id ON messages (id);
CREATE INDEX IF NOT EXISTS messages_by_chat_time ON messages (chat_jid, timestamp);
CREATE INDEX IF NOT EXISTS messages_by_time ON messages (timestamp);
CREATE INDEX IF NOT EXISTS messages_by_sender_time ON messages (sender, timestamp);

-- A sender's current reaction to a message; removing a reaction deletes the row.
CREATE TABLE IF NOT EXISTS message_reactions (
	chat_jid TEXT NOT NULL,
	target_message_id TEXT NOT NULL,
	target_sender TEXT NOT NULL DEFAULT '',
	reaction_sender TEXT NOT NULL,
	emoji TEXT NOT NULL,
	reaction_message_id TEXT,
	grouping_key TEXT,
	sender_timestamp_ms INTEGER,
	timestamp INTEGER,
	is_from_me INTEGER NOT NULL DEFAULT 0,
	PRIMARY KEY (chat_jid, target_message_id, reaction_sender)
);

CREATE TABLE IF NOT EXISTS message_receipts (
	message_id TEXT NOT NULL,
	chat_jid TEXT NOT NULL,
	receipt_type TEXT NOT NULL,
	receipt_sender TEXT NOT NULL,
	message_sender TEXT NOT NULL DEFAULT '',
	timestamp INTEGER,
	PRIMARY KEY (chat_jid, message_id, receipt_type, receipt_sender, message_sender)
);

-- The account's LID <-> phone-number map (user parts, as whatsmeow stores them)
-- and its own JID without a device suffix: identity, not session secrets.
CREATE TABLE IF NOT EXISTS lid_map (
	lid TEXT PRIMARY KEY,
	pn TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lid_map_by_pn ON lid_map (pn);

CREATE TABLE IF NOT EXISTS self (
	id INTEGER PRIMARY KEY CHECK (id = 1),
	jid TEXT NOT NULL
);
