package main

import (
	"database/sql"
	_ "embed"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// schemaVersion marks messages.db files laid out by schema.sql (epoch-millisecond
// times). The pre-0.11.14 layout kept text timestamps and user_version 0.
const schemaVersion = 2

//go:embed schema.sql
var schemaSQL string

// MessageStore keeps WhatsApp chats, messages, reactions, receipts, media
// metadata and identity in messages.db, a SQLite file in the store directory
// next to the whatsmeow session store.
type MessageStore struct {
	db *sql.DB

	identityMu     sync.Mutex
	identitySynced bool
	syncedSelf     string
	syncedLIDs     int
}

func messagesDBPath() string {
	return filepath.Join(getStoreDir(), "messages.db")
}

// NewMessageStore opens (creating when absent) messages.db in the store directory.
func NewMessageStore() (*MessageStore, error) {
	storeDir := getStoreDir()
	if err := os.MkdirAll(storeDir, 0700); err != nil {
		return nil, fmt.Errorf("failed to create store directory: %v", err)
	}

	path := messagesDBPath()
	db, err := sql.Open("sqlite3", fmt.Sprintf("file:%s?_journal_mode=WAL&_busy_timeout=10000&_synchronous=NORMAL", path))
	if err != nil {
		return nil, fmt.Errorf("failed to open %s: %v", path, err)
	}
	// One writer connection serializes writes from concurrent event handlers.
	db.SetMaxOpenConns(1)

	if err := ensureSchema(db, path); err != nil {
		db.Close()
		return nil, err
	}
	return &MessageStore{db: db}, nil
}

func ensureSchema(db *sql.DB, path string) error {
	var version int
	if err := db.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		return fmt.Errorf("failed to read %s schema version: %v", path, err)
	}
	if version == schemaVersion {
		_, err := db.Exec(schemaSQL)
		return err
	}
	if version != 0 {
		return fmt.Errorf("%s has schema version %d; this bridge understands %d", path, version, schemaVersion)
	}
	var tables int
	if err := db.QueryRow("SELECT count(*) FROM sqlite_master WHERE type = 'table'").Scan(&tables); err != nil {
		return err
	}
	if tables > 0 {
		return fmt.Errorf("%s uses the pre-0.11.14 layout; convert it with scripts/migrate_store.py before starting the bridge", path)
	}
	if _, err := db.Exec(schemaSQL); err != nil {
		return fmt.Errorf("failed to create tables: %v", err)
	}
	_, err := db.Exec(fmt.Sprintf("PRAGMA user_version = %d", schemaVersion))
	return err
}

// Close closes the database.
func (store *MessageStore) Close() error {
	return store.db.Close()
}

// Optional text and bytes are NULL when empty; times are epoch milliseconds.
func nullString(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func nullBytes(value []byte) any {
	if len(value) == 0 {
		return nil
	}
	return value
}

func nullMillis(value time.Time) any {
	if value.IsZero() {
		return nil
	}
	return value.UnixMilli()
}

func nullLength(value uint64) any {
	if value == 0 {
		return nil
	}
	return int64(value)
}

func epochMillis(value time.Time) int64 {
	if value.IsZero() {
		return 0
	}
	return value.UnixMilli()
}

func boolInt(value bool) int {
	if value {
		return 1
	}
	return 0
}

// StoreChat records a chat's name and last message time, replacing what was stored.
func (store *MessageStore) StoreChat(jid, name string, lastMessageTime time.Time) error {
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		jid, nullString(name), nullMillis(lastMessageTime),
	)
	return err
}

// ChatName reports a chat's stored name ("" when the chat is unknown or has none).
func (store *MessageStore) ChatName(jid string) (string, error) {
	var name sql.NullString
	err := store.db.QueryRow("SELECT name FROM chats WHERE jid = ?", jid).Scan(&name)
	if err == sql.ErrNoRows {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return name.String, nil
}

// StoreMessage inserts or replaces a message by (chat, id); a message with
// neither text nor media is skipped.
func (store *MessageStore) StoreMessage(id, chatJID, sender, content string, timestamp time.Time, isFromMe bool,
	mediaType string, reply ReplyMetadata, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	if content == "" && mediaType == "" {
		return nil
	}

	_, err := store.db.Exec(
		`INSERT OR REPLACE INTO messages
		(id, chat_jid, sender, content, timestamp, is_from_me, media_type, reply_to_message_id, reply_to_sender,
		 reply_to_content, reply_to_media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, chatJID, sender, content, epochMillis(timestamp), boolInt(isFromMe), nullString(mediaType),
		nullString(reply.MessageID), nullString(reply.Sender), nullString(reply.Content), nullString(reply.MediaType),
		nullString(filename), nullString(url), nullBytes(mediaKey), nullBytes(fileSHA256), nullBytes(fileEncSHA256),
		nullLength(fileLength),
	)
	return err
}

// ApplyMessageEdit revises a message already on record: it replaces the text and
// stamps edited_at, and replaces media fields only when the edit actually
// carries media. Fields the edit says nothing about — the reply it answered,
// the media a caption belonged to — are left standing. Reports whether a stored
// message matched.
func (store *MessageStore) ApplyMessageEdit(
	id, chatJID, content string, editedAt time.Time, mediaType, filename, url string,
	mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64,
) (bool, error) {
	if id == "" || chatJID == "" {
		return false, nil
	}

	query := "UPDATE messages SET content = ?, edited_at = ?"
	args := []any{content, epochMillis(editedAt)}
	if mediaType != "" {
		query += ", media_type = ?, filename = ?, url = ?, media_key = ?, file_sha256 = ?, file_enc_sha256 = ?, file_length = ?"
		args = append(args, mediaType, nullString(filename), nullString(url), nullBytes(mediaKey),
			nullBytes(fileSHA256), nullBytes(fileEncSHA256), nullLength(fileLength))
	}
	query += " WHERE chat_jid = ? AND id = ?"
	args = append(args, chatJID, id)

	result, err := store.db.Exec(query, args...)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

// StoreReaction records a sender's current reaction; an empty emoji removes it.
func (store *MessageStore) StoreReaction(reaction ReactionMetadata) error {
	if reaction.ChatJID == "" || reaction.TargetMessageID == "" || reaction.Sender == "" {
		return nil
	}

	if reaction.Emoji == "" {
		_, err := store.db.Exec(
			"DELETE FROM message_reactions WHERE chat_jid = ? AND target_message_id = ? AND reaction_sender = ?",
			reaction.ChatJID, reaction.TargetMessageID, reaction.Sender,
		)
		return err
	}

	var senderTimestampMS any
	if reaction.SenderTimestampMS != 0 {
		senderTimestampMS = reaction.SenderTimestampMS
	}
	_, err := store.db.Exec(
		`INSERT OR REPLACE INTO message_reactions
		(chat_jid, target_message_id, target_sender, reaction_sender, emoji, reaction_message_id, grouping_key,
		 sender_timestamp_ms, timestamp, is_from_me)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		reaction.ChatJID, reaction.TargetMessageID, reaction.TargetSender, reaction.Sender, reaction.Emoji,
		nullString(reaction.ReactionMessageID), nullString(reaction.GroupingKey), senderTimestampMS,
		nullMillis(reaction.Timestamp), boolInt(reaction.IsFromMe),
	)
	return err
}

func normalizeReceiptType(receiptType types.ReceiptType) string {
	raw := string(receiptType)
	if raw == "" {
		return "delivered"
	}
	return raw
}

// StoreReceipt records one delivery, read or played receipt.
func (store *MessageStore) StoreReceipt(messageID, chatJID, receiptType, receiptSender, messageSender string, timestamp time.Time) error {
	if messageID == "" || chatJID == "" || receiptType == "" || receiptSender == "" {
		return nil
	}

	_, err := store.db.Exec(
		`INSERT OR REPLACE INTO message_receipts
		(message_id, chat_jid, receipt_type, receipt_sender, message_sender, timestamp)
		VALUES (?, ?, ?, ?, ?, ?)`,
		messageID, chatJID, receiptType, receiptSender, messageSender, nullMillis(timestamp),
	)
	return err
}

func (store *MessageStore) GetStoredMessageTimestamp(id, chatJID string) (time.Time, bool, error) {
	var timestamp int64
	err := store.db.QueryRow("SELECT timestamp FROM messages WHERE chat_jid = ? AND id = ?", chatJID, id).Scan(&timestamp)
	if err == sql.ErrNoRows {
		return time.Time{}, false, nil
	}
	if err != nil {
		return time.Time{}, false, err
	}
	return time.UnixMilli(timestamp), true, nil
}

// GetMessages returns a chat's messages, newest first.
func (store *MessageStore) GetMessages(chatJID string, limit int) ([]Message, error) {
	rows, err := store.db.Query(
		`SELECT sender, content, timestamp, is_from_me, coalesce(media_type, ''), coalesce(filename, '')
		FROM messages WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT ?`,
		chatJID, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var messages []Message
	for rows.Next() {
		var msg Message
		var timestamp int64
		if err := rows.Scan(&msg.Sender, &msg.Content, &timestamp, &msg.IsFromMe, &msg.MediaType, &msg.Filename); err != nil {
			return nil, err
		}
		msg.Time = time.UnixMilli(timestamp)
		messages = append(messages, msg)
	}
	return messages, rows.Err()
}

// GetChats returns every chat's last message time (zero when unknown).
func (store *MessageStore) GetChats() (map[string]time.Time, error) {
	rows, err := store.db.Query("SELECT jid, last_message_time FROM chats ORDER BY last_message_time DESC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	chats := make(map[string]time.Time)
	for rows.Next() {
		var jid string
		var last sql.NullInt64
		if err := rows.Scan(&jid, &last); err != nil {
			return nil, err
		}
		if last.Valid && last.Int64 != 0 {
			chats[jid] = time.UnixMilli(last.Int64)
		} else {
			chats[jid] = time.Time{}
		}
	}
	return chats, rows.Err()
}

// StoreMediaInfo records the download metadata of a stored message.
func (store *MessageStore) StoreMediaInfo(id, chatJID, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	_, err := store.db.Exec(
		`UPDATE messages SET url = ?, media_key = ?, file_sha256 = ?, file_enc_sha256 = ?, file_length = ?
		WHERE chat_jid = ? AND id = ?`,
		nullString(url), nullBytes(mediaKey), nullBytes(fileSHA256), nullBytes(fileEncSHA256), nullLength(fileLength),
		chatJID, id,
	)
	return err
}

// GetMediaInfo reads a message's media metadata; an unknown message reports sql.ErrNoRows.
func (store *MessageStore) GetMediaInfo(id, chatJID string) (string, string, string, []byte, []byte, []byte, uint64, error) {
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength int64
	err := store.db.QueryRow(
		`SELECT coalesce(media_type, ''), coalesce(filename, ''), coalesce(url, ''), media_key, file_sha256,
		file_enc_sha256, coalesce(file_length, 0)
		FROM messages WHERE chat_jid = ? AND id = ?`,
		chatJID, id,
	).Scan(&mediaType, &filename, &url, &mediaKey, &fileSHA256, &fileEncSHA256, &fileLength)
	if err != nil {
		return "", "", "", nil, nil, nil, 0, err
	}
	if fileLength < 0 {
		fileLength = 0
	}
	return mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, uint64(fileLength), nil
}

// LIDPair maps a WhatsApp LID user to its phone-number user, as whatsmeow stores it.
type LIDPair struct {
	LID string `json:"lid"`
	PN  string `json:"pn"`
}

// SetIdentity records the account's own JID (when known) and its LID <-> phone map.
func (store *MessageStore) SetIdentity(self string, lids []LIDPair) error {
	tx, err := store.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	for _, pair := range lids {
		if _, err := tx.Exec(
			"INSERT INTO lid_map (lid, pn) VALUES (?, ?) ON CONFLICT (lid) DO UPDATE SET pn = excluded.pn",
			pair.LID, pair.PN,
		); err != nil {
			return err
		}
	}
	if self != "" {
		if _, err := tx.Exec("INSERT OR REPLACE INTO self (id, jid) VALUES (1, ?)", self); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// readLIDMap reads whatsmeow's LID <-> phone map from the session store, read-only.
func readLIDMap() ([]LIDPair, error) {
	db, err := sql.Open("sqlite3", fmt.Sprintf("file:%s?mode=ro", filepath.Join(getStoreDir(), "whatsapp.db")))
	if err != nil {
		return nil, err
	}
	defer db.Close()

	rows, err := db.Query("SELECT lid, pn FROM whatsmeow_lid_map")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var pairs []LIDPair
	for rows.Next() {
		var pair LIDPair
		if err := rows.Scan(&pair.LID, &pair.PN); err != nil {
			return nil, err
		}
		if pair.LID != "" && pair.PN != "" {
			pairs = append(pairs, pair)
		}
	}
	return pairs, rows.Err()
}

// syncIdentity records the account's own JID (without device suffix) and the LID
// map, skipping the write when neither has changed since the last successful sync.
func syncIdentity(client *whatsmeow.Client, store *MessageStore, logger waLog.Logger) {
	if store == nil {
		return
	}
	self := ""
	if client != nil && client.Store != nil && client.Store.ID != nil {
		self = client.Store.ID.User + "@" + client.Store.ID.Server
	}
	lids, err := readLIDMap()
	if err != nil {
		logger.Warnf("Failed to read LID map: %v", err)
	}
	if self == "" && len(lids) == 0 {
		return
	}

	store.identityMu.Lock()
	defer store.identityMu.Unlock()
	if store.identitySynced && self == store.syncedSelf && len(lids) == store.syncedLIDs {
		return
	}
	if err := store.SetIdentity(self, lids); err != nil {
		logger.Warnf("Failed to store identity: %v", err)
		return
	}
	store.identitySynced = true
	store.syncedSelf = self
	store.syncedLIDs = len(lids)
	logger.Infof("Stored identity with %d LID mappings", len(lids))
}
