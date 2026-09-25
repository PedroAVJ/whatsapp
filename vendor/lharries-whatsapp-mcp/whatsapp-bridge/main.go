package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"math"
	"math/rand"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal"
	"rsc.io/qr"

	"bytes"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	waStore "go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// Message represents a chat message for our client
type Message struct {
	Time      time.Time
	Sender    string
	Content   string
	IsFromMe  bool
	MediaType string
	Filename  string
}

type ReplyMetadata struct {
	MessageID string
	Sender    string
	Content   string
	MediaType string
}

type ReactionMetadata struct {
	ReactionMessageID string
	ChatJID           string
	TargetMessageID   string
	TargetSender      string
	Sender            string
	Emoji             string
	Timestamp         time.Time
	GroupingKey       string
	SenderTimestampMS int64
	IsFromMe          bool
}

// MessageStore keeps WhatsApp data in Near's Convex deployment over its HTTP API.
type MessageStore struct {
	baseURL string
	token   string
	client  *http.Client

	identityMu     sync.Mutex
	identitySynced bool
	syncedSelf     string
	syncedLIDs     int
}

func getEnvOrDefault(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func getStoreDir() string {
	return getEnvOrDefault("WHATSAPP_MCP_STORE_DIR", "store")
}

func getHTTPPort() int {
	raw := getEnvOrDefault("WHATSAPP_MCP_HTTP_PORT", "8080")
	port, err := strconv.Atoi(raw)
	if err != nil || port <= 0 {
		return 8080
	}
	return port
}

func shouldExitAfterAuth() bool {
	raw := strings.ToLower(strings.TrimSpace(os.Getenv("WHATSAPP_MCP_EXIT_AFTER_AUTH")))
	return raw == "1" || raw == "true" || raw == "yes"
}

func shouldLogMessageContent() bool {
	raw := strings.ToLower(strings.TrimSpace(os.Getenv("WHATSAPP_MCP_LOG_MESSAGE_CONTENT")))
	return raw == "1" || raw == "true" || raw == "yes"
}

func shouldRequestFullHistorySync() bool {
	raw := strings.ToLower(strings.TrimSpace(os.Getenv("WHATSAPP_MCP_REQUEST_FULL_HISTORY")))
	return raw == "1" || raw == "true" || raw == "yes"
}

func getInitialHistorySyncTimeout() time.Duration {
	raw := getEnvOrDefault("WHATSAPP_MCP_HISTORY_SYNC_TIMEOUT_SECS", "600")
	seconds, err := strconv.Atoi(raw)
	if err != nil || seconds <= 0 {
		seconds = 600
	}
	return time.Duration(seconds) * time.Second
}

func getInitialHistorySyncGracePeriod() time.Duration {
	raw := getEnvOrDefault("WHATSAPP_MCP_HISTORY_SYNC_GRACE_SECS", "15")
	seconds, err := strconv.Atoi(raw)
	if err != nil || seconds < 0 {
		seconds = 15
	}
	return time.Duration(seconds) * time.Second
}

// configureFullHistorySync asks WhatsApp for the largest linked-device history
// it permits during a fresh pairing. WhatsApp still enforces its own recency and
// size ceilings, so this is a maximum-history request rather than an archival
// account export.
func configureFullHistorySync() {
	waStore.DeviceProps.RequireFullSync = proto.Bool(true)
}

func fullHistorySyncCompleted(historySync *events.HistorySync) bool {
	return historySync != nil &&
		historySync.Data != nil &&
		historySync.Data.GetSyncType() == waHistorySync.HistorySync_FULL &&
		historySync.Data.GetProgress() >= 100
}

func getQRTextPath() string {
	return strings.TrimSpace(os.Getenv("WHATSAPP_MCP_QR_TEXT_PATH"))
}

func getQRPNGPath() string {
	return strings.TrimSpace(os.Getenv("WHATSAPP_MCP_QR_PNG_PATH"))
}

func getPairPhoneNumber() string {
	return strings.TrimSpace(os.Getenv("WHATSAPP_MCP_PAIR_PHONE"))
}

func getPairPhoneDisplayName() string {
	return getEnvOrDefault("WHATSAPP_MCP_PAIR_DISPLAY_NAME", "Chrome (Linux)")
}

func writeQRCodePNG(codeText, outputPath string) error {
	code, err := qr.Encode(strings.TrimSpace(codeText), qr.L)
	if err != nil {
		return err
	}

	const scale = 12
	const quietZone = 4
	size := (code.Size + quietZone*2) * scale
	img := image.NewRGBA(image.Rect(0, 0, size, size))
	white := color.RGBA{255, 255, 255, 255}
	black := color.RGBA{0, 0, 0, 255}

	for y := 0; y < size; y++ {
		for x := 0; x < size; x++ {
			img.Set(x, y, white)
		}
	}

	for moduleY := 0; moduleY < code.Size; moduleY++ {
		for moduleX := 0; moduleX < code.Size; moduleX++ {
			if !code.Black(moduleX, moduleY) {
				continue
			}
			startX := (moduleX + quietZone) * scale
			startY := (moduleY + quietZone) * scale
			for y := startY; y < startY+scale; y++ {
				for x := startX; x < startX+scale; x++ {
					img.Set(x, y, black)
				}
			}
		}
	}

	if err := os.MkdirAll(filepath.Dir(outputPath), 0755); err != nil {
		return err
	}

	output, err := os.Create(outputPath)
	if err != nil {
		return err
	}
	defer output.Close()

	return png.Encode(output, img)
}

func saveQRCodeArtifacts(codeText string) {
	if qrTextPath := getQRTextPath(); qrTextPath != "" {
		if err := os.WriteFile(qrTextPath, []byte(codeText), 0644); err == nil {
			fmt.Printf("Saved QR text to %s\n", qrTextPath)
		} else {
			fmt.Printf("Failed to save QR text to %s: %v\n", qrTextPath, err)
		}
	}

	if qrPNGPath := getQRPNGPath(); qrPNGPath != "" {
		if err := writeQRCodePNG(codeText, qrPNGPath); err == nil {
			fmt.Printf("Saved QR PNG to %s\n", qrPNGPath)
		} else {
			fmt.Printf("Failed to save QR PNG to %s: %v\n", qrPNGPath, err)
		}
	}
}

// Convex identifies the Convex deployment that holds WhatsApp data and the write
// token the bridge presents on every call.
func getConvexURL() string {
	return strings.TrimRight(getEnvOrDefault("WHATSAPP_CONVEX_URL", "http://127.0.0.1:3210"), "/")
}

// resolveConvexWriteToken reads the write token from the environment, else once
// from the macOS Keychain.
func resolveConvexWriteToken() (string, error) {
	if token := strings.TrimSpace(os.Getenv("WHATSAPP_CONVEX_WRITE_TOKEN")); token != "" {
		return token, nil
	}
	out, err := exec.Command("security", "find-generic-password", "-a", "near", "-s", "n4.convex-write", "-w").Output()
	if token := strings.TrimSpace(string(out)); err == nil && token != "" {
		return token, nil
	}
	return "", fmt.Errorf("no Convex write token: set WHATSAPP_CONVEX_WRITE_TOKEN or add the Keychain item (account near, service n4.convex-write)")
}

// Initialize message store
func NewMessageStore() (*MessageStore, error) {
	token, err := resolveConvexWriteToken()
	if err != nil {
		return nil, err
	}
	return &MessageStore{
		baseURL: getConvexURL(),
		token:   token,
		client:  &http.Client{Timeout: 30 * time.Second},
	}, nil
}

// call runs one Convex function ("query" or "mutation") and decodes its value into out.
func (store *MessageStore) call(kind, name string, args map[string]any, out any) error {
	args["token"] = store.token
	body, err := json.Marshal(map[string]any{"path": "whatsapp:" + name, "args": args, "format": "json"})
	if err != nil {
		return err
	}
	resp, err := store.client.Post(store.baseURL+"/api/"+kind, "application/json", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("convex whatsapp:%s: %v", name, err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("convex whatsapp:%s: %v", name, err)
	}

	var result struct {
		Status       string          `json:"status"`
		Value        json.RawMessage `json:"value"`
		ErrorMessage string          `json:"errorMessage"`
	}
	if err := json.Unmarshal(raw, &result); err != nil || result.Status != "success" {
		message := result.ErrorMessage
		if message == "" {
			message = fmt.Sprintf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
		}
		// Argument-validation errors can echo the arguments; never log the token.
		message = strings.ReplaceAll(message, store.token, "[token]")
		if len(message) > 500 {
			message = message[:500] + "..."
		}
		return fmt.Errorf("convex whatsapp:%s: %s", name, message)
	}
	if out != nil {
		return json.Unmarshal(result.Value, out)
	}
	return nil
}

func (store *MessageStore) mutation(name string, args map[string]any, out any) error {
	return store.call("mutation", name, args, out)
}

func (store *MessageStore) query(name string, args map[string]any, out any) error {
	return store.call("query", name, args, out)
}

// Optional Convex fields are omitted when empty; times are epoch milliseconds and
// binary fields base64.
func putString(args map[string]any, key, value string) {
	if value != "" {
		args[key] = value
	}
}

func putBytes(args map[string]any, key string, value []byte) {
	if len(value) > 0 {
		args[key] = base64.StdEncoding.EncodeToString(value)
	}
}

func putTime(args map[string]any, key string, value time.Time) {
	if !value.IsZero() {
		args[key] = value.UnixMilli()
	}
}

func putLength(args map[string]any, key string, value uint64) {
	if value > 0 {
		args[key] = value
	}
}

func epochMillis(value time.Time) int64 {
	if value.IsZero() {
		return 0
	}
	return value.UnixMilli()
}

func fromEpochMillis(value float64) time.Time {
	return time.UnixMilli(int64(math.Round(value)))
}

func decodeBase64(value string) []byte {
	if value == "" {
		return nil
	}
	decoded, err := base64.StdEncoding.DecodeString(value)
	if err != nil {
		return nil
	}
	return decoded
}

// Close releases idle connections to Convex.
func (store *MessageStore) Close() error {
	store.client.CloseIdleConnections()
	return nil
}

// Store a chat in Convex
func (store *MessageStore) StoreChat(jid, name string, lastMessageTime time.Time) error {
	args := map[string]any{"jid": jid}
	putString(args, "name", name)
	putTime(args, "lastMessageTime", lastMessageTime)
	return store.mutation("upsertChat", args, nil)
}

// ChatName reports a chat's stored name ("" when the chat has none) and whether the chat is known.
func (store *MessageStore) ChatName(jid string) (string, error) {
	var name *string
	if err := store.query("chatName", map[string]any{"jid": jid}, &name); err != nil {
		return "", err
	}
	if name == nil {
		return "", nil
	}
	return *name, nil
}

// Store a message in Convex
func (store *MessageStore) StoreMessage(id, chatJID, sender, content string, timestamp time.Time, isFromMe bool,
	mediaType string, reply ReplyMetadata, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	// Only store if there's actual content or media
	if content == "" && mediaType == "" {
		return nil
	}

	message := map[string]any{
		"id":        id,
		"chatJid":   chatJID,
		"sender":    sender,
		"content":   content,
		"timestamp": epochMillis(timestamp),
		"isFromMe":  isFromMe,
	}
	putString(message, "mediaType", mediaType)
	putString(message, "replyToMessageId", reply.MessageID)
	putString(message, "replyToSender", reply.Sender)
	putString(message, "replyToContent", reply.Content)
	putString(message, "replyToMediaType", reply.MediaType)
	putString(message, "filename", filename)
	putString(message, "url", url)
	putBytes(message, "mediaKey", mediaKey)
	putBytes(message, "fileSha256", fileSHA256)
	putBytes(message, "fileEncSha256", fileEncSHA256)
	putLength(message, "fileLength", fileLength)
	return store.mutation("storeMessages", map[string]any{"messages": []map[string]any{message}}, nil)
}

// ApplyMessageEdit revises a message already on record: it replaces the text and
// stamps editedAt, and replaces media fields only when the edit actually
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

	args := map[string]any{
		"id":       id,
		"chatJid":  chatJID,
		"content":  content,
		"editedAt": epochMillis(editedAt),
	}
	if mediaType != "" {
		putString(args, "mediaType", mediaType)
		putString(args, "filename", filename)
		putString(args, "url", url)
		putBytes(args, "mediaKey", mediaKey)
		putBytes(args, "fileSha256", fileSHA256)
		putBytes(args, "fileEncSha256", fileEncSHA256)
		putLength(args, "fileLength", fileLength)
	}

	var updated bool
	if err := store.mutation("applyEdit", args, &updated); err != nil {
		return false, err
	}
	return updated, nil
}

// StoreReaction records a sender's current reaction; an empty emoji removes it.
func (store *MessageStore) StoreReaction(reaction ReactionMetadata) error {
	if reaction.ChatJID == "" || reaction.TargetMessageID == "" || reaction.Sender == "" {
		return nil
	}

	value := map[string]any{
		"chatJid":         reaction.ChatJID,
		"targetMessageId": reaction.TargetMessageID,
		"targetSender":    reaction.TargetSender,
		"reactionSender":  reaction.Sender,
		"emoji":           reaction.Emoji,
		"isFromMe":        reaction.IsFromMe,
	}
	putString(value, "reactionMessageId", reaction.ReactionMessageID)
	putString(value, "groupingKey", reaction.GroupingKey)
	if reaction.SenderTimestampMS != 0 {
		value["senderTimestampMs"] = reaction.SenderTimestampMS
	}
	putTime(value, "timestamp", reaction.Timestamp)
	return store.mutation("storeReaction", map[string]any{"reaction": value}, nil)
}

func normalizeReceiptType(receiptType types.ReceiptType) string {
	raw := string(receiptType)
	if raw == "" {
		return "delivered"
	}
	return raw
}

func (store *MessageStore) StoreReceipt(messageID, chatJID, receiptType, receiptSender, messageSender string, timestamp time.Time) error {
	if messageID == "" || chatJID == "" || receiptType == "" || receiptSender == "" {
		return nil
	}

	value := map[string]any{
		"messageId":     messageID,
		"chatJid":       chatJID,
		"receiptType":   receiptType,
		"receiptSender": receiptSender,
		"messageSender": messageSender,
	}
	putTime(value, "timestamp", timestamp)
	return store.mutation("storeReceipt", map[string]any{"receipt": value}, nil)
}

func (store *MessageStore) GetStoredMessageTimestamp(id, chatJID string) (time.Time, bool, error) {
	var timestamp *float64
	if err := store.query("storedTimestamp", map[string]any{"id": id, "chatJid": chatJID}, &timestamp); err != nil {
		return time.Time{}, false, err
	}
	if timestamp == nil {
		return time.Time{}, false, nil
	}
	return fromEpochMillis(*timestamp), true, nil
}

// Get messages from a chat, newest first
func (store *MessageStore) GetMessages(chatJID string, limit int) ([]Message, error) {
	var rows []struct {
		Sender    string  `json:"sender"`
		Content   string  `json:"content"`
		Timestamp float64 `json:"timestamp"`
		IsFromMe  bool    `json:"isFromMe"`
		MediaType string  `json:"mediaType"`
		Filename  string  `json:"filename"`
	}
	if err := store.query("recentMessages", map[string]any{"chatJid": chatJID, "limit": limit}, &rows); err != nil {
		return nil, err
	}

	var messages []Message
	for _, row := range rows {
		messages = append(messages, Message{
			Time:      fromEpochMillis(row.Timestamp),
			Sender:    row.Sender,
			Content:   row.Content,
			IsFromMe:  row.IsFromMe,
			MediaType: row.MediaType,
			Filename:  row.Filename,
		})
	}
	return messages, nil
}

// Get all chats
func (store *MessageStore) GetChats() (map[string]time.Time, error) {
	var rows []struct {
		JID             string  `json:"jid"`
		LastMessageTime float64 `json:"lastMessageTime"`
	}
	if err := store.query("chatTimes", map[string]any{}, &rows); err != nil {
		return nil, err
	}

	chats := make(map[string]time.Time)
	for _, row := range rows {
		if row.LastMessageTime == 0 {
			chats[row.JID] = time.Time{}
			continue
		}
		chats[row.JID] = fromEpochMillis(row.LastMessageTime)
	}
	return chats, nil
}

// LIDPair maps a WhatsApp LID user to its phone-number user, as whatsmeow stores it.
type LIDPair struct {
	LID string `json:"lid"`
	PN  string `json:"pn"`
}

// SetIdentity records the account's own JID and its LID <-> phone map in batches.
func (store *MessageStore) SetIdentity(self string, lids []LIDPair) error {
	const batchSize = 500
	for start := 0; start == 0 || start < len(lids); start += batchSize {
		end := min(start+batchSize, len(lids))
		args := map[string]any{"lids": append([]LIDPair{}, lids[start:end]...)}
		if start == 0 {
			putString(args, "self", self)
		}
		if err := store.mutation("setIdentity", args, nil); err != nil {
			return err
		}
	}
	return nil
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

// syncIdentity sends the account's own JID (without device suffix) and the LID map
// to Convex, skipping the map when it has not grown since the last successful sync.
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

type visibleTextCollector struct {
	parts []string
	seen  map[string]struct{}
}

func (collector *visibleTextCollector) add(values ...string) {
	if collector.seen == nil {
		collector.seen = make(map[string]struct{})
	}
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		if _, exists := collector.seen[value]; exists {
			continue
		}
		collector.seen[value] = struct{}{}
		collector.parts = append(collector.parts, value)
	}
}

func (collector *visibleTextCollector) String() string {
	return strings.Join(collector.parts, "\n")
}

func appendHydratedTemplateText(collector *visibleTextCollector, template *waProto.TemplateMessage_HydratedFourRowTemplate) {
	if template == nil {
		return
	}

	collector.add(
		template.GetHydratedTitleText(),
		template.GetHydratedContentText(),
		template.GetHydratedFooterText(),
	)
	for _, button := range template.GetHydratedButtons() {
		if button == nil {
			continue
		}
		switch {
		case button.GetQuickReplyButton() != nil:
			collector.add(button.GetQuickReplyButton().GetDisplayText())
		case button.GetUrlButton() != nil:
			collector.add(button.GetUrlButton().GetDisplayText())
		case button.GetCallButton() != nil:
			collector.add(button.GetCallButton().GetDisplayText())
		}
	}
}

func appendHighlyStructuredText(collector *visibleTextCollector, message *waProto.HighlyStructuredMessage, depth int) {
	if message == nil || depth >= 8 {
		return
	}
	appendTemplateText(collector, message.GetHydratedHsm(), depth+1)
}

func appendFourRowTemplateText(collector *visibleTextCollector, template *waProto.TemplateMessage_FourRowTemplate, depth int) {
	if template == nil || depth >= 8 {
		return
	}

	appendHighlyStructuredText(collector, template.GetHighlyStructuredMessage(), depth+1)
	appendHighlyStructuredText(collector, template.GetContent(), depth+1)
	appendHighlyStructuredText(collector, template.GetFooter(), depth+1)
	for _, button := range template.GetButtons() {
		if button == nil {
			continue
		}
		switch {
		case button.GetQuickReplyButton() != nil:
			appendHighlyStructuredText(collector, button.GetQuickReplyButton().GetDisplayText(), depth+1)
		case button.GetUrlButton() != nil:
			appendHighlyStructuredText(collector, button.GetUrlButton().GetDisplayText(), depth+1)
		case button.GetCallButton() != nil:
			appendHighlyStructuredText(collector, button.GetCallButton().GetDisplayText(), depth+1)
		}
	}
}

func appendInteractiveText(collector *visibleTextCollector, message *waProto.InteractiveMessage, depth int) {
	if message == nil || depth >= 8 {
		return
	}

	header := message.GetHeader()
	collector.add(header.GetTitle(), header.GetSubtitle())
	collector.add(message.GetBody().GetText())
	collector.add(message.GetFooter().GetText())
	for _, card := range message.GetCarouselMessage().GetCards() {
		appendInteractiveText(collector, card, depth+1)
	}
}

func appendTemplateText(collector *visibleTextCollector, message *waProto.TemplateMessage, depth int) {
	if message == nil || depth >= 8 {
		return
	}

	appendHydratedTemplateText(collector, message.GetHydratedTemplate())
	appendHydratedTemplateText(collector, message.GetHydratedFourRowTemplate())
	appendFourRowTemplateText(collector, message.GetFourRowTemplate(), depth+1)
	appendInteractiveText(collector, message.GetInteractiveMessageTemplate(), depth+1)
}

func appendListText(collector *visibleTextCollector, message *waProto.ListMessage) {
	if message == nil {
		return
	}

	collector.add(message.GetTitle(), message.GetDescription(), message.GetButtonText(), message.GetFooterText())
	for _, section := range message.GetSections() {
		if section == nil {
			continue
		}
		collector.add(section.GetTitle())
		for _, row := range section.GetRows() {
			if row != nil {
				collector.add(row.GetTitle(), row.GetDescription())
			}
		}
	}
}

func appendButtonsText(collector *visibleTextCollector, message *waProto.ButtonsMessage) {
	if message == nil {
		return
	}

	collector.add(message.GetText(), message.GetContentText(), message.GetFooterText())
	for _, button := range message.GetButtons() {
		if button != nil {
			collector.add(button.GetButtonText().GetDisplayText())
		}
	}
}

// Extract text content from a message. Business messages carry their visible
// text in template, list, button, and interactive payloads instead of the
// standard conversation fields, so those payloads must be read explicitly.
// Only user-visible strings are collected; routing IDs, template IDs, URLs,
// phone numbers, and opaque JSON parameters remain excluded.
func extractTextContent(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}

	// Try to get text content from standard text messages first.
	if text := msg.GetConversation(); text != "" {
		return text
	} else if extendedText := msg.GetExtendedTextMessage(); extendedText != nil {
		if text := extendedText.GetText(); text != "" {
			return text
		}
	}

	// Media captions are also user-authored message content.
	if img := msg.GetImageMessage(); img != nil {
		if caption := img.GetCaption(); caption != "" {
			return caption
		}
	}

	if vid := msg.GetVideoMessage(); vid != nil {
		if caption := vid.GetCaption(); caption != "" {
			return caption
		}
	}

	if doc := msg.GetDocumentMessage(); doc != nil {
		if caption := doc.GetCaption(); caption != "" {
			return caption
		}
	}

	collector := &visibleTextCollector{}
	appendTemplateText(collector, msg.GetTemplateMessage(), 0)
	appendListText(collector, msg.GetListMessage())
	appendInteractiveText(collector, msg.GetInteractiveMessage(), 0)
	appendButtonsText(collector, msg.GetButtonsMessage())
	collector.add(msg.GetButtonsResponseMessage().GetSelectedDisplayText())
	collector.add(msg.GetListResponseMessage().GetTitle(), msg.GetListResponseMessage().GetDescription())
	collector.add(msg.GetTemplateButtonReplyMessage().GetSelectedDisplayText())
	collector.add(msg.GetInteractiveResponseMessage().GetBody().GetText())
	if text := collector.String(); text != "" {
		return text
	}

	return ""
}

func extractMessageMediaType(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}

	if msg.GetImageMessage() != nil {
		return "image"
	}
	if msg.GetVideoMessage() != nil {
		return "video"
	}
	if msg.GetAudioMessage() != nil {
		return "audio"
	}
	if msg.GetDocumentMessage() != nil {
		return "document"
	}
	if msg.GetStickerMessage() != nil {
		return "sticker"
	}

	return ""
}

func extractContextInfo(msg *waProto.Message) *waProto.ContextInfo {
	if msg == nil {
		return nil
	}

	if extendedText := msg.GetExtendedTextMessage(); extendedText != nil && extendedText.GetContextInfo() != nil {
		return extendedText.GetContextInfo()
	}
	if img := msg.GetImageMessage(); img != nil && img.GetContextInfo() != nil {
		return img.GetContextInfo()
	}
	if vid := msg.GetVideoMessage(); vid != nil && vid.GetContextInfo() != nil {
		return vid.GetContextInfo()
	}
	if aud := msg.GetAudioMessage(); aud != nil && aud.GetContextInfo() != nil {
		return aud.GetContextInfo()
	}
	if doc := msg.GetDocumentMessage(); doc != nil && doc.GetContextInfo() != nil {
		return doc.GetContextInfo()
	}
	if sticker := msg.GetStickerMessage(); sticker != nil && sticker.GetContextInfo() != nil {
		return sticker.GetContextInfo()
	}
	if location := msg.GetLocationMessage(); location != nil && location.GetContextInfo() != nil {
		return location.GetContextInfo()
	}
	if contact := msg.GetContactMessage(); contact != nil && contact.GetContextInfo() != nil {
		return contact.GetContextInfo()
	}
	if template := msg.GetTemplateMessage(); template != nil && template.GetContextInfo() != nil {
		return template.GetContextInfo()
	}
	if list := msg.GetListMessage(); list != nil && list.GetContextInfo() != nil {
		return list.GetContextInfo()
	}
	if interactive := msg.GetInteractiveMessage(); interactive != nil && interactive.GetContextInfo() != nil {
		return interactive.GetContextInfo()
	}
	if buttons := msg.GetButtonsMessage(); buttons != nil && buttons.GetContextInfo() != nil {
		return buttons.GetContextInfo()
	}
	if buttonsResponse := msg.GetButtonsResponseMessage(); buttonsResponse != nil && buttonsResponse.GetContextInfo() != nil {
		return buttonsResponse.GetContextInfo()
	}
	if listResponse := msg.GetListResponseMessage(); listResponse != nil && listResponse.GetContextInfo() != nil {
		return listResponse.GetContextInfo()
	}
	if templateResponse := msg.GetTemplateButtonReplyMessage(); templateResponse != nil && templateResponse.GetContextInfo() != nil {
		return templateResponse.GetContextInfo()
	}
	if interactiveResponse := msg.GetInteractiveResponseMessage(); interactiveResponse != nil && interactiveResponse.GetContextInfo() != nil {
		return interactiveResponse.GetContextInfo()
	}

	return nil
}

func extractReplyMetadata(msg *waProto.Message) ReplyMetadata {
	contextInfo := extractContextInfo(msg)
	if contextInfo == nil {
		return ReplyMetadata{}
	}

	quotedMessage := contextInfo.GetQuotedMessage()
	return ReplyMetadata{
		MessageID: contextInfo.GetStanzaID(),
		Sender:    contextInfo.GetParticipant(),
		Content:   extractTextContent(quotedMessage),
		MediaType: extractMessageMediaType(quotedMessage),
	}
}

func sanitizeMessageID(messageID string) string {
	replacer := strings.NewReplacer(
		"/", "_",
		"\\", "_",
		":", "_",
	)

	sanitized := strings.TrimSpace(replacer.Replace(messageID))
	if sanitized == "" {
		return "unknown"
	}

	return sanitized
}

func buildMediaFilename(mediaType, messageID, originalFilename string) string {
	idPart := sanitizeMessageID(messageID)

	switch mediaType {
	case "image":
		return "image_" + idPart + ".jpg"
	case "video":
		return "video_" + idPart + ".mp4"
	case "audio":
		return "audio_" + idPart + ".ogg"
	case "document":
		filename := strings.TrimSpace(filepath.Base(originalFilename))
		if filename != "" && filename != "." {
			return filename
		}
		return "document_" + idPart
	default:
		return originalFilename
	}
}

func resolveDownloadFilename(mediaType, messageID, storedFilename string) string {
	filename := buildMediaFilename(mediaType, messageID, storedFilename)
	if strings.TrimSpace(filename) != "" {
		return filename
	}

	return sanitizeMessageID(messageID)
}

func fileMatchesStoredSHA256(localPath string, expected []byte) (bool, error) {
	data, err := os.ReadFile(localPath)
	if err != nil {
		return false, err
	}

	actual := sha256.Sum256(data)
	return bytes.Equal(actual[:], expected), nil
}

// SendMessageResponse represents the response for the send message API
type SendMessageResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
}

// SendMessageRequest represents the request body for the send message API
type SendMessageRequest struct {
	Recipient        string `json:"recipient"`
	Message          string `json:"message"`
	MediaPath        string `json:"media_path,omitempty"`
	ReplyToMessageID string `json:"reply_to_message_id,omitempty"`
	ReplyToSender    string `json:"reply_to_sender,omitempty"`
	ReplyToContent   string `json:"reply_to_content,omitempty"`
	ReplyToMediaType string `json:"reply_to_media_type,omitempty"`
}

func senderToParticipant(sender string) string {
	sender = strings.TrimSpace(sender)
	if sender == "" || strings.Contains(sender, "@") {
		return sender
	}
	if hasStoredLID(sender) {
		return sender + "@" + types.HiddenUserServer
	}
	return sender + "@" + types.DefaultUserServer
}

func hasStoredLID(user string) bool {
	if strings.TrimSpace(user) == "" {
		return false
	}

	db, err := sql.Open("sqlite3", fmt.Sprintf("file:%s?mode=ro", filepath.Join(getStoreDir(), "whatsapp.db")))
	if err != nil {
		return false
	}
	defer db.Close()

	var lid string
	err = db.QueryRow("SELECT lid FROM whatsmeow_lid_map WHERE lid = ? LIMIT 1", user).Scan(&lid)
	return err == nil && lid != ""
}

func messageSenderJID(sender types.JID, chat types.JID) string {
	if sender.User != "" && sender.Server != "" {
		return sender.String()
	}
	if chat.User != "" && chat.Server != "" {
		return chat.String()
	}
	return ""
}

func reactionSenderJID(evt *events.Message) string {
	if evt == nil {
		return ""
	}
	if evt.Info.IsFromMe {
		return "me"
	}
	return messageSenderJID(evt.Info.Sender, evt.Info.Chat)
}

func reactionTargetChatJID(reaction *waProto.ReactionMessage, fallback types.JID) string {
	if reaction == nil {
		return ""
	}
	if key := reaction.GetKey(); key != nil {
		if remoteJID := key.GetRemoteJID(); remoteJID != "" {
			return remoteJID
		}
	}
	return fallback.String()
}

func reactionTargetSender(reaction *waProto.ReactionMessage) string {
	if reaction == nil {
		return ""
	}

	key := reaction.GetKey()
	if key == nil {
		return ""
	}
	if participant := key.GetParticipant(); participant != "" {
		return participant
	}
	if key.GetFromMe() {
		return "me"
	}
	return ""
}

func extractReactionMetadata(client *whatsmeow.Client, evt *events.Message, messageID string, msg *waProto.Message, logger waLog.Logger) (ReactionMetadata, bool) {
	if evt == nil || msg == nil {
		return ReactionMetadata{}, false
	}

	reaction := msg.GetReactionMessage()
	if reaction == nil && msg.GetEncReactionMessage() != nil && client != nil {
		decrypted, err := client.DecryptReaction(context.Background(), evt)
		if err != nil {
			logger.Warnf("Failed to decrypt reaction message %s: %v", messageID, err)
			return ReactionMetadata{}, false
		}
		reaction = decrypted
	}
	if reaction == nil {
		return ReactionMetadata{}, false
	}

	targetMessageID := reaction.GetKey().GetID()
	if targetMessageID == "" {
		return ReactionMetadata{}, false
	}

	timestamp := evt.Info.Timestamp
	senderTimestampMS := reaction.GetSenderTimestampMS()
	if timestamp.IsZero() && senderTimestampMS > 0 {
		timestamp = time.UnixMilli(senderTimestampMS)
	}

	return ReactionMetadata{
		ReactionMessageID: messageID,
		ChatJID:           reactionTargetChatJID(reaction, evt.Info.Chat),
		TargetMessageID:   targetMessageID,
		TargetSender:      reactionTargetSender(reaction),
		Sender:            reactionSenderJID(evt),
		Emoji:             reaction.GetText(),
		Timestamp:         timestamp,
		GroupingKey:       reaction.GetGroupingKey(),
		SenderTimestampMS: senderTimestampMS,
		IsFromMe:          evt.Info.IsFromMe,
	}, true
}

func quotedMessageFromReply(reply ReplyMetadata) *waProto.Message {
	content := reply.Content
	switch reply.MediaType {
	case "image":
		return &waProto.Message{ImageMessage: &waProto.ImageMessage{
			Caption: proto.String(content),
		}}
	case "video":
		return &waProto.Message{VideoMessage: &waProto.VideoMessage{
			Caption: proto.String(content),
		}}
	case "audio":
		return &waProto.Message{AudioMessage: &waProto.AudioMessage{}}
	case "document":
		return &waProto.Message{DocumentMessage: &waProto.DocumentMessage{
			Title: proto.String(content),
		}}
	default:
		if strings.TrimSpace(content) == "" {
			return nil
		}
		return &waProto.Message{Conversation: proto.String(content)}
	}
}

func buildReplyContext(reply ReplyMetadata) *waProto.ContextInfo {
	if strings.TrimSpace(reply.MessageID) == "" {
		return nil
	}

	return &waProto.ContextInfo{
		StanzaID:      proto.String(reply.MessageID),
		Participant:   proto.String(senderToParticipant(reply.Sender)),
		QuotedMessage: quotedMessageFromReply(reply),
	}
}

func applyReplyContext(msg *waProto.Message, contextInfo *waProto.ContextInfo) {
	if msg == nil || contextInfo == nil {
		return
	}

	if extendedText := msg.GetExtendedTextMessage(); extendedText != nil {
		extendedText.ContextInfo = contextInfo
		return
	}
	if imageMessage := msg.GetImageMessage(); imageMessage != nil {
		imageMessage.ContextInfo = contextInfo
		return
	}
	if videoMessage := msg.GetVideoMessage(); videoMessage != nil {
		videoMessage.ContextInfo = contextInfo
		return
	}
	if audioMessage := msg.GetAudioMessage(); audioMessage != nil {
		audioMessage.ContextInfo = contextInfo
		return
	}
	if documentMessage := msg.GetDocumentMessage(); documentMessage != nil {
		documentMessage.ContextInfo = contextInfo
		return
	}
}

func resolveOutboundMediaType(mediaPath string) (whatsmeow.MediaType, string) {
	switch strings.ToLower(filepath.Ext(mediaPath)) {
	case ".jpg", ".jpeg":
		return whatsmeow.MediaImage, "image/jpeg"
	case ".png":
		return whatsmeow.MediaImage, "image/png"
	case ".gif":
		return whatsmeow.MediaImage, "image/gif"
	case ".webp":
		return whatsmeow.MediaImage, "image/webp"
	case ".ogg":
		return whatsmeow.MediaAudio, "audio/ogg; codecs=opus"
	case ".mp4":
		return whatsmeow.MediaVideo, "video/mp4"
	case ".avi":
		return whatsmeow.MediaVideo, "video/avi"
	case ".mov":
		return whatsmeow.MediaVideo, "video/quicktime"
	case ".pdf":
		return whatsmeow.MediaDocument, "application/pdf"
	default:
		return whatsmeow.MediaDocument, "application/octet-stream"
	}
}

// Function to send a WhatsApp message
func sendWhatsAppMessage(client *whatsmeow.Client, messageStore *MessageStore, recipient string, message string, mediaPath string, reply ReplyMetadata, logger waLog.Logger) (bool, string) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp"
	}

	// Create JID for recipient
	var recipientJID types.JID
	var err error

	// Check if recipient is a JID
	isJID := strings.Contains(recipient, "@")

	if isJID {
		// Parse the JID string
		recipientJID, err = types.ParseJID(recipient)
		if err != nil {
			return false, fmt.Sprintf("Error parsing JID: %v", err)
		}
	} else {
		// Create JID from phone number
		recipientJID = types.JID{
			User:   recipient,
			Server: "s.whatsapp.net", // For personal chats
		}
	}

	msg := &waProto.Message{}
	replyContext := buildReplyContext(reply)

	// Check if we have media to send
	if mediaPath != "" {
		// Read media file
		mediaData, err := os.ReadFile(mediaPath)
		if err != nil {
			return false, fmt.Sprintf("Error reading media file: %v", err)
		}

		mediaType, mimeType := resolveOutboundMediaType(mediaPath)

		// Upload media to WhatsApp servers
		resp, err := client.Upload(context.Background(), mediaData, mediaType)
		if err != nil {
			return false, fmt.Sprintf("Error uploading media: %v", err)
		}

		fmt.Println("Media uploaded", resp)

		// Create the appropriate message type based on media type
		switch mediaType {
		case whatsmeow.MediaImage:
			msg.ImageMessage = &waProto.ImageMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaAudio:
			// Handle ogg audio files
			var seconds uint32 = 30 // Default fallback
			var waveform []byte = nil

			// Try to analyze the ogg file
			if strings.Contains(mimeType, "ogg") {
				analyzedSeconds, analyzedWaveform, err := analyzeOggOpus(mediaData)
				if err == nil {
					seconds = analyzedSeconds
					waveform = analyzedWaveform
				} else {
					return false, fmt.Sprintf("Failed to analyze Ogg Opus file: %v", err)
				}
			} else {
				fmt.Printf("Not an Ogg Opus file: %s\n", mimeType)
			}

			msg.AudioMessage = &waProto.AudioMessage{
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				Seconds:       proto.Uint32(seconds),
				PTT:           proto.Bool(true),
				Waveform:      waveform,
			}
		case whatsmeow.MediaVideo:
			msg.VideoMessage = &waProto.VideoMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaDocument:
			msg.DocumentMessage = &waProto.DocumentMessage{
				Title:         proto.String(mediaPath[strings.LastIndex(mediaPath, "/")+1:]),
				FileName:      proto.String(mediaPath[strings.LastIndex(mediaPath, "/")+1:]),
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		}
	} else {
		if replyContext != nil {
			msg.ExtendedTextMessage = &waProto.ExtendedTextMessage{
				Text:        proto.String(message),
				ContextInfo: replyContext,
			}
		} else {
			msg.Conversation = proto.String(message)
		}
	}
	applyReplyContext(msg, replyContext)

	// Send message
	resp, err := client.SendMessage(context.Background(), recipientJID, msg)

	if err != nil {
		return false, fmt.Sprintf("Error sending message: %v", err)
	}

	sender := ""
	if client.Store.ID != nil {
		sender = client.Store.ID.ToNonAD().String()
	}
	if err := storeSentMessage(messageStore, sender, recipientJID, resp.ID, resp.Timestamp, msg, reply); err != nil {
		fmt.Printf("Failed to store sent message %s: %v\n", resp.ID, err)
	} else {
		mediaType, _, _, _, _, _, _ := extractMediaInfo(msg, resp.ID)
		if mediaType != "" {
			runMediaArrivalHook(mediaType, resp.ID, recipientJID.String(), logger)
		}
	}

	return true, fmt.Sprintf("Message sent to %s", recipient)
}

// Persist a message this bridge just sent. whatsmeow emits no Message event
// for the client's own sends, so without this explicit insert outbound bridge
// messages never reach the store.
func storeSentMessage(store *MessageStore, sender string, chat types.JID, messageID string, timestamp time.Time, msg *waProto.Message, reply ReplyMetadata) error {
	if store == nil || messageID == "" || msg == nil {
		return nil
	}

	chatJID := chat.String()
	name := chat.User
	if existingName, err := store.ChatName(chatJID); err == nil && existingName != "" {
		name = existingName
	}
	if err := store.StoreChat(chatJID, name, timestamp); err != nil {
		return err
	}

	content := extractTextContent(msg)
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength := extractMediaInfo(msg, messageID)
	return store.StoreMessage(messageID, chatJID, sender, content, timestamp, true, mediaType, reply, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength)
}

// Extract media info from a message
func extractMediaInfo(msg *waProto.Message, messageID string) (mediaType string, filename string, url string, mediaKey []byte, fileSHA256 []byte, fileEncSHA256 []byte, fileLength uint64) {
	if msg == nil {
		return "", "", "", nil, nil, nil, 0
	}

	// Check for image message
	if img := msg.GetImageMessage(); img != nil {
		return "image", buildMediaFilename("image", messageID, ""),
			img.GetURL(), img.GetMediaKey(), img.GetFileSHA256(), img.GetFileEncSHA256(), img.GetFileLength()
	}

	// Check for video message
	if vid := msg.GetVideoMessage(); vid != nil {
		return "video", buildMediaFilename("video", messageID, ""),
			vid.GetURL(), vid.GetMediaKey(), vid.GetFileSHA256(), vid.GetFileEncSHA256(), vid.GetFileLength()
	}

	// Check for audio message
	if aud := msg.GetAudioMessage(); aud != nil {
		return "audio", buildMediaFilename("audio", messageID, ""),
			aud.GetURL(), aud.GetMediaKey(), aud.GetFileSHA256(), aud.GetFileEncSHA256(), aud.GetFileLength()
	}

	// Check for document message
	if doc := msg.GetDocumentMessage(); doc != nil {
		filename := buildMediaFilename("document", messageID, doc.GetFileName())
		return "document", filename,
			doc.GetURL(), doc.GetMediaKey(), doc.GetFileSHA256(), doc.GetFileEncSHA256(), doc.GetFileLength()
	}

	return "", "", "", nil, nil, nil, 0
}

// unwrapMessageLayers peels the container messages WhatsApp uses to carry a
// payload. whatsmeow unwraps most of these already, but an edit can arrive with
// its protocol message nested one layer deeper (an edit of a disappearing or
// view-once message, or an edit echoed from another of our own devices). Reading
// GetProtocolMessage() off the outermost message misses those, and the edit is
// then stored as an empty message — which is to say, dropped.
func unwrapMessageLayers(msg *waProto.Message) *waProto.Message {
	// Bounded: a real payload nests a handful of layers deep at most, and the
	// bound keeps a malformed or hostile message from spinning here.
	for range 8 {
		switch {
		case msg.GetDeviceSentMessage().GetMessage() != nil:
			msg = msg.GetDeviceSentMessage().GetMessage()
		case msg.GetEphemeralMessage().GetMessage() != nil:
			msg = msg.GetEphemeralMessage().GetMessage()
		case msg.GetViewOnceMessage().GetMessage() != nil:
			msg = msg.GetViewOnceMessage().GetMessage()
		case msg.GetViewOnceMessageV2().GetMessage() != nil:
			msg = msg.GetViewOnceMessageV2().GetMessage()
		case msg.GetViewOnceMessageV2Extension().GetMessage() != nil:
			msg = msg.GetViewOnceMessageV2Extension().GetMessage()
		case msg.GetDocumentWithCaptionMessage().GetMessage() != nil:
			msg = msg.GetDocumentWithCaptionMessage().GetMessage()
		case msg.GetEditedMessage().GetMessage() != nil:
			msg = msg.GetEditedMessage().GetMessage()
		default:
			return msg
		}
	}

	return msg
}

// normalizeMessageForStorage resolves the message ID and payload to persist. For
// an edit both change: the edit carries its own stanza ID, but it must be stored
// against the ID of the message it revises, with the revised payload.
func normalizeMessageForStorage(evt *events.Message) (string, *waProto.Message, bool) {
	if evt == nil {
		return "", nil, false
	}

	messageID := string(evt.Info.ID)
	normalized := evt.Message
	if normalized == nil {
		return messageID, nil, false
	}

	normalized = unwrapMessageLayers(normalized)

	protocolMessage := normalized.GetProtocolMessage()
	// evt.IsEdit is whatsmeow's own verdict after unwrapping. Trust either
	// signal: a protocol message typed MESSAGE_EDIT, or an event whatsmeow
	// already recognised as an edit. Requiring both drops edits whose type
	// field is absent.
	isEdit := protocolMessage.GetType() == waProto.ProtocolMessage_MESSAGE_EDIT || evt.IsEdit
	if !isEdit || protocolMessage == nil {
		return messageID, normalized, false
	}

	if key := protocolMessage.GetKey(); key != nil && key.GetID() != "" {
		messageID = key.GetID()
	}
	if editedMessage := protocolMessage.GetEditedMessage(); editedMessage != nil {
		normalized = unwrapMessageLayers(editedMessage)
	}

	return messageID, normalized, true
}

// describeMessagePayload names the populated top-level fields of a message
// without reading their values. It exists so an unstorable message leaves a
// diagnosable trace in the log instead of vanishing, and it must never widen
// into logging message content.
func describeMessagePayload(msg *waProto.Message) string {
	if msg == nil {
		return "<nil>"
	}

	fields := []string{}
	msg.ProtoReflect().Range(func(fd protoreflect.FieldDescriptor, _ protoreflect.Value) bool {
		fields = append(fields, string(fd.Name()))
		return true
	})
	if len(fields) == 0 {
		return "<empty>"
	}
	sort.Strings(fields)

	return strings.Join(fields, ",")
}

func storeEventMessage(client *whatsmeow.Client, messageStore *MessageStore, evt *events.Message, logger waLog.Logger) error {
	if evt == nil {
		return nil
	}

	messageID, normalizedMessage, isEdit := normalizeMessageForStorage(evt)
	if messageID == "" || normalizedMessage == nil {
		return nil
	}

	if reaction, ok := extractReactionMetadata(client, evt, messageID, normalizedMessage, logger); ok {
		return messageStore.StoreReaction(reaction)
	}

	content := extractTextContent(normalizedMessage)
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength := extractMediaInfo(normalizedMessage, messageID)
	reply := extractReplyMetadata(normalizedMessage)

	if shouldLogMessageContent() {
		logger.Infof("Message content: %v, Media Type: %v", content, mediaType)
	}

	if content == "" && mediaType == "" {
		// Nothing storable. Say so: an edit that silently produced no content is
		// exactly how a revised message goes missing with no trace to diagnose.
		// Field names only — this log must never carry message content.
		logger.Warnf(
			"Storing nothing for message %s in %s (edit=%v): no text or media in payload fields [%s]",
			messageID,
			evt.Info.Chat.String(),
			isEdit,
			describeMessagePayload(normalizedMessage),
		)
		return nil
	}

	chatJID := evt.Info.Chat.String()
	sender := messageSenderJID(evt.Info.Sender, evt.Info.Chat)

	if isEdit {
		// An edit revises a message already on record. Writing a whole row would
		// blank the columns the edit does not carry — the reply it answered, the
		// media it was a caption for — so revise in place instead, and record
		// that the message was edited.
		updated, err := messageStore.ApplyMessageEdit(
			messageID,
			chatJID,
			content,
			evt.Info.Timestamp,
			mediaType,
			filename,
			url,
			mediaKey,
			fileSHA256,
			fileEncSHA256,
			fileLength,
		)
		if err != nil {
			return err
		}
		if updated {
			return nil
		}
		// The original never landed (edited before we synced it, or dropped by an
		// earlier bug). Fall through and store it as a new row so the current text
		// is on record either way.
		logger.Warnf("Edit for unknown message %s in %s; storing edited content as a new row", messageID, chatJID)
	}

	timestamp := evt.Info.Timestamp
	if isEdit {
		storedTimestamp, found, err := messageStore.GetStoredMessageTimestamp(messageID, chatJID)
		if err != nil {
			logger.Warnf("Failed to get stored timestamp for edited message %s: %v", messageID, err)
		} else if found {
			timestamp = storedTimestamp
		}
	}

	return messageStore.StoreMessage(
		messageID,
		chatJID,
		sender,
		content,
		timestamp,
		evt.Info.IsFromMe,
		mediaType,
		reply,
		filename,
		url,
		mediaKey,
		fileSHA256,
		fileEncSHA256,
		fileLength,
	)
}

// Handle regular incoming messages with media support
func handleMessage(client *whatsmeow.Client, messageStore *MessageStore, msg *events.Message, logger waLog.Logger) {
	// Save message to the store
	chatJID := msg.Info.Chat.String()
	sender := msg.Info.Sender.User

	// Get appropriate chat name (pass nil for conversation since we don't have one for regular messages)
	name := GetChatName(client, messageStore, msg.Info.Chat, chatJID, nil, directChatNameHintsFromMessage(msg.Info), logger)

	// Update the chat with the message timestamp (keeps last message time updated)
	err := messageStore.StoreChat(chatJID, name, msg.Info.Timestamp)
	if err != nil {
		logger.Warnf("Failed to store chat: %v", err)
	}

	err = storeEventMessage(client, messageStore, msg, logger)
	if err != nil {
		logger.Warnf("Failed to store message: %v", err)
	} else {
		messageID, normalizedMessage, _ := normalizeMessageForStorage(msg)
		content := extractTextContent(normalizedMessage)
		mediaType, filename, _, _, _, _, _ := extractMediaInfo(normalizedMessage, messageID)

		// Log message reception
		timestamp := msg.Info.Timestamp.Format("2006-01-02 15:04:05")
		direction := "←"
		if msg.Info.IsFromMe {
			direction = "→"
		}

		// Log based on message type
		if mediaType != "" {
			fmt.Printf("[%s] %s %s: [%s: %s] %s\n", timestamp, direction, sender, mediaType, filename, content)
		} else if content != "" {
			fmt.Printf("[%s] %s %s: %s\n", timestamp, direction, sender, content)
		}

		if mediaType != "" {
			runMediaArrivalHook(mediaType, messageID, chatJID, logger)
		}
	}
}

// runMediaArrivalHook fires the configured WhatsApp CLI executable when media
// lands. The bridge supplies the fixed "media arrival-hook" subcommand and
// message arguments, avoiding shell parsing and keeping paths with spaces safe.
func runMediaArrivalHook(mediaType, messageID, chatJID string, logger waLog.Logger) {
	hook := strings.TrimSpace(os.Getenv("WHATSAPP_MEDIA_ARRIVAL_HOOK"))
	if hook == "" || messageID == "" || chatJID == "" {
		return
	}

	wanted := strings.TrimSpace(os.Getenv("WHATSAPP_MEDIA_ARRIVAL_HOOK_TYPES"))
	if wanted == "" {
		wanted = "audio"
	}
	matched := false
	for _, candidate := range strings.Split(wanted, ",") {
		if strings.EqualFold(strings.TrimSpace(candidate), mediaType) {
			matched = true
			break
		}
	}
	if !matched {
		return
	}

	// Detached: a slow or wedged transcription must never stall the event loop.
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
		defer cancel()

		cmd := exec.CommandContext(ctx, hook, "media", "arrival-hook", mediaType, messageID, chatJID)
		cmd.Stdout = nil
		cmd.Stderr = nil
		if err := cmd.Run(); err != nil {
			logger.Warnf("Media arrival hook failed for %s (%s): %v", messageID, mediaType, err)
		}
	}()
}

var mediaReconcileRunning atomic.Bool
var mediaReconcileRequested atomic.Bool

// runMediaReconcileHook drains the durable message store once when the bridge
// starts or reconnects. This covers downtime and history-sync gaps without a
// second daemon polling beside the bridge.
func runMediaReconcileHook(logger waLog.Logger) {
	hook := strings.TrimSpace(os.Getenv("WHATSAPP_MEDIA_RECONCILE_HOOK"))
	if hook == "" {
		return
	}
	mediaReconcileRequested.Store(true)
	if !mediaReconcileRunning.CompareAndSwap(false, true) {
		return
	}

	limit := 50
	if configured := strings.TrimSpace(os.Getenv("WHATSAPP_MEDIA_RECONCILE_LIMIT")); configured != "" {
		if parsed, err := strconv.Atoi(configured); err == nil && parsed > 0 {
			limit = parsed
		}
	}
	lookbackDays := 14
	if configured := strings.TrimSpace(os.Getenv("WHATSAPP_MEDIA_RECONCILE_LOOKBACK_DAYS")); configured != "" {
		if parsed, err := strconv.Atoi(configured); err == nil && parsed > 0 {
			lookbackDays = parsed
		}
	}
	since := time.Now().UTC().AddDate(0, 0, -lookbackDays).Format("2006-01-02")
	reconcileTimeout := 4 * time.Hour
	if configured := strings.TrimSpace(os.Getenv("WHATSAPP_MEDIA_RECONCILE_TIMEOUT_MINUTES")); configured != "" {
		if parsed, err := strconv.Atoi(configured); err == nil && parsed > 0 {
			reconcileTimeout = time.Duration(parsed) * time.Minute
		}
	}

	go func() {
		defer func() {
			mediaReconcileRunning.Store(false)
			if mediaReconcileRequested.Load() {
				runMediaReconcileHook(logger)
			}
		}()

		for mediaReconcileRequested.Swap(false) {
			ctx, cancel := context.WithTimeout(context.Background(), reconcileTimeout)
			cmd := exec.CommandContext(
				ctx,
				hook,
				"media", "transcribe-pending",
				"--since", since,
				"--limit", strconv.Itoa(limit),
				"--drain",
			)
			cmd.Stdout = nil
			cmd.Stderr = nil
			err := cmd.Run()
			cancel()
			if err != nil {
				logger.Warnf("Media reconciliation hook failed: %v", err)
			}
		}
	}()
}

func handleReceipt(messageStore *MessageStore, receipt *events.Receipt, logger waLog.Logger) {
	if receipt == nil {
		return
	}

	chatJID := receipt.Chat.String()
	receiptSender := receipt.Sender.String()
	if receiptSender == "" {
		receiptSender = chatJID
	}
	messageSender := receipt.MessageSender.String()
	receiptType := normalizeReceiptType(receipt.Type)

	for _, messageID := range receipt.MessageIDs {
		if err := messageStore.StoreReceipt(string(messageID), chatJID, receiptType, receiptSender, messageSender, receipt.Timestamp); err != nil {
			logger.Warnf("Failed to store receipt for message %s: %v", messageID, err)
		}
	}
}

// DownloadMediaRequest represents the request body for the download media API
type DownloadMediaRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid"`
}

// DownloadMediaResponse represents the response for the download media API
type DownloadMediaResponse struct {
	Success  bool   `json:"success"`
	Message  string `json:"message"`
	Filename string `json:"filename,omitempty"`
	Path     string `json:"path,omitempty"`
}

// Store additional media info in Convex
func (store *MessageStore) StoreMediaInfo(id, chatJID, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	args := map[string]any{"id": id, "chatJid": chatJID}
	putString(args, "url", url)
	putBytes(args, "mediaKey", mediaKey)
	putBytes(args, "fileSha256", fileSHA256)
	putBytes(args, "fileEncSha256", fileEncSHA256)
	putLength(args, "fileLength", fileLength)
	return store.mutation("storeMediaInfo", args, nil)
}

// Get media info from Convex; an unknown message reports sql.ErrNoRows as the SQLite store did.
func (store *MessageStore) GetMediaInfo(id, chatJID string) (string, string, string, []byte, []byte, []byte, uint64, error) {
	var info *struct {
		MediaType     string  `json:"mediaType"`
		Filename      string  `json:"filename"`
		URL           string  `json:"url"`
		MediaKey      string  `json:"mediaKey"`
		FileSHA256    string  `json:"fileSha256"`
		FileEncSHA256 string  `json:"fileEncSha256"`
		FileLength    float64 `json:"fileLength"`
	}
	if err := store.query("mediaInfo", map[string]any{"id": id, "chatJid": chatJID}, &info); err != nil {
		return "", "", "", nil, nil, nil, 0, err
	}
	if info == nil {
		return "", "", "", nil, nil, nil, 0, sql.ErrNoRows
	}
	return info.MediaType, info.Filename, info.URL, decodeBase64(info.MediaKey), decodeBase64(info.FileSHA256),
		decodeBase64(info.FileEncSHA256), uint64(math.Max(0, info.FileLength)), nil
}

// MediaDownloader implements the whatsmeow.DownloadableMessage interface
type MediaDownloader struct {
	URL           string
	DirectPath    string
	MediaKey      []byte
	FileLength    uint64
	FileSHA256    []byte
	FileEncSHA256 []byte
	MediaType     whatsmeow.MediaType
}

// GetDirectPath implements the DownloadableMessage interface
func (d *MediaDownloader) GetDirectPath() string {
	return d.DirectPath
}

// GetURL implements the DownloadableMessage interface
func (d *MediaDownloader) GetURL() string {
	return d.URL
}

// GetMediaKey implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaKey() []byte {
	return d.MediaKey
}

// GetFileLength implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileLength() uint64 {
	return d.FileLength
}

// GetFileSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileSHA256() []byte {
	return d.FileSHA256
}

// GetFileEncSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileEncSHA256() []byte {
	return d.FileEncSHA256
}

// GetMediaType implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaType() whatsmeow.MediaType {
	return d.MediaType
}

// Function to download media from a message
func downloadMedia(client *whatsmeow.Client, messageStore *MessageStore, messageID, chatJID string) (bool, string, string, string, error) {
	// Look up the message in the store
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64
	var err error

	// First, check if we already have this file
	chatDir := filepath.Join(getStoreDir(), strings.ReplaceAll(chatJID, ":", "_"))
	localPath := ""

	// Get media info from the store
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err = messageStore.GetMediaInfo(messageID, chatJID)

	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to find message: %v", err)
	}

	// Check if this is a media message
	if mediaType == "" {
		return false, "", "", "", fmt.Errorf("not a media message")
	}

	// Create directory for the chat if it doesn't exist
	if err := os.MkdirAll(chatDir, 0755); err != nil {
		return false, "", "", "", fmt.Errorf("failed to create chat directory: %v", err)
	}

	// Recompute the expected local filename from the message ID so stale DB rows
	// or older bridge builds cannot collapse multiple media messages onto one path.
	filename = resolveDownloadFilename(mediaType, messageID, filename)

	// Generate a local path for the file
	localPath = filepath.Join(chatDir, filename)

	// Get absolute path
	absPath, err := filepath.Abs(localPath)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to get absolute path: %v", err)
	}

	// Check if file already exists
	if _, err := os.Stat(localPath); err == nil {
		if len(fileSHA256) == 0 {
			return true, mediaType, filename, absPath, nil
		}

		matches, hashErr := fileMatchesStoredSHA256(localPath, fileSHA256)
		if hashErr == nil && matches {
			return true, mediaType, filename, absPath, nil
		}

		if removeErr := os.Remove(localPath); removeErr != nil && !os.IsNotExist(removeErr) {
			return false, "", "", "", fmt.Errorf("failed to remove stale media file: %v", removeErr)
		}
	}

	// If we don't have all the media info we need, we can't download
	if url == "" || len(mediaKey) == 0 || len(fileSHA256) == 0 || len(fileEncSHA256) == 0 || fileLength == 0 {
		return false, "", "", "", fmt.Errorf("incomplete media information for download")
	}

	fmt.Printf("Attempting to download media for message %s in chat %s...\n", messageID, chatJID)

	// Extract direct path from URL
	directPath := extractDirectPathFromURL(url)

	// Create a downloader that implements DownloadableMessage
	var waMediaType whatsmeow.MediaType
	switch mediaType {
	case "image":
		waMediaType = whatsmeow.MediaImage
	case "video":
		waMediaType = whatsmeow.MediaVideo
	case "audio":
		waMediaType = whatsmeow.MediaAudio
	case "document":
		waMediaType = whatsmeow.MediaDocument
	default:
		return false, "", "", "", fmt.Errorf("unsupported media type: %s", mediaType)
	}

	downloader := &MediaDownloader{
		URL:           url,
		DirectPath:    directPath,
		MediaKey:      mediaKey,
		FileLength:    fileLength,
		FileSHA256:    fileSHA256,
		FileEncSHA256: fileEncSHA256,
		MediaType:     waMediaType,
	}

	// Download the media using whatsmeow client
	mediaData, err := client.Download(context.Background(), downloader)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to download media: %v", err)
	}

	// Save the downloaded media to file
	if err := os.WriteFile(localPath, mediaData, 0644); err != nil {
		return false, "", "", "", fmt.Errorf("failed to save media file: %v", err)
	}

	fmt.Printf("Successfully downloaded %s media to %s (%d bytes)\n", mediaType, absPath, len(mediaData))
	return true, mediaType, filename, absPath, nil
}

// Extract direct path from a WhatsApp media URL
func extractDirectPathFromURL(url string) string {
	// The direct path is typically in the URL, we need to extract it
	// Example URL: https://mmg.whatsapp.net/v/t62.7118-24/13812002_698058036224062_3424455886509161511_n.enc?ccb=11-4&oh=...

	// Find the path part after the domain
	parts := strings.SplitN(url, ".net/", 2)
	if len(parts) < 2 {
		return url // Return original URL if parsing fails
	}

	pathPart := parts[1]

	// Whatsmeow appends download parameters to the direct path with '&', so
	// preserve the URL's query string (including the initial '?').
	return "/" + pathPart
}

// Start a REST API server to expose the WhatsApp client functionality
func startRESTServer(client *whatsmeow.Client, messageStore *MessageStore, port int, logger waLog.Logger) {
	mux := http.NewServeMux()

	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"ok":          true,
			"connected":   client.IsConnected(),
			"store_dir":   getStoreDir(),
			"http_port":   port,
			"logged_in":   client.Store.ID != nil,
			"description": "whatsapp-bridge health",
		})
	})

	// Handler for sending messages
	mux.HandleFunc("/api/send", func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req SendMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.Recipient == "" {
			http.Error(w, "Recipient is required", http.StatusBadRequest)
			return
		}

		if req.Message == "" && req.MediaPath == "" {
			http.Error(w, "Message or media path is required", http.StatusBadRequest)
			return
		}

		fmt.Println("Received request to send message", req.Message, req.MediaPath)

		// Send the message
		reply := ReplyMetadata{
			MessageID: req.ReplyToMessageID,
			Sender:    req.ReplyToSender,
			Content:   req.ReplyToContent,
			MediaType: req.ReplyToMediaType,
		}
		success, message := sendWhatsAppMessage(client, messageStore, req.Recipient, req.Message, req.MediaPath, reply, logger)
		fmt.Println("Message sent", success, message)
		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Set appropriate status code
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}

		// Send response
		json.NewEncoder(w).Encode(SendMessageResponse{
			Success: success,
			Message: message,
		})
	})

	// Handler for downloading media
	mux.HandleFunc("/api/download", func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req DownloadMediaRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "Message ID and Chat JID are required", http.StatusBadRequest)
			return
		}

		// Download the media
		success, mediaType, filename, path, err := downloadMedia(client, messageStore, req.MessageID, req.ChatJID)

		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Handle download result
		if !success || err != nil {
			errMsg := "Unknown error"
			if err != nil {
				errMsg = err.Error()
			}

			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(DownloadMediaResponse{
				Success: false,
				Message: fmt.Sprintf("Failed to download media: %s", errMsg),
			})
			return
		}

		// Send successful response
		json.NewEncoder(w).Encode(DownloadMediaResponse{
			Success:  true,
			Message:  fmt.Sprintf("Successfully downloaded %s media", mediaType),
			Filename: filename,
			Path:     path,
		})
	})

	// Start the server
	serverAddr := fmt.Sprintf("127.0.0.1:%d", port) // localhost only: the REST API has no authentication
	fmt.Printf("Starting REST API server on %s...\n", serverAddr)

	// Run server in a goroutine so it doesn't block
	go func() {
		if err := http.ListenAndServe(serverAddr, mux); err != nil {
			fmt.Printf("REST API server error: %v\n", err)
		}
	}()
}

func main() {
	// Set up logger
	logger := waLog.Stdout("Client", "INFO", true)
	logger.Infof("Starting WhatsApp client...")

	// Create database connection for storing session data
	dbLog := waLog.Stdout("Database", "INFO", true)

	// Create directory for database if it doesn't exist
	storeDir := getStoreDir()
	if err := os.MkdirAll(storeDir, 0755); err != nil {
		logger.Errorf("Failed to create store directory: %v", err)
		return
	}

	whatsappDBPath := filepath.Join(storeDir, "whatsapp.db")
	container, err := sqlstore.New(context.Background(), "sqlite3", fmt.Sprintf("file:%s?_foreign_keys=on", whatsappDBPath), dbLog)
	if err != nil {
		logger.Errorf("Failed to connect to database: %v", err)
		return
	}

	// Get device store - This contains session information
	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		if err == sql.ErrNoRows {
			// No device exists, create one
			deviceStore = container.NewDevice()
			logger.Infof("Created new device")
		} else {
			logger.Errorf("Failed to get device: %v", err)
			return
		}
	}
	requestingFullHistory := deviceStore.ID == nil && shouldRequestFullHistorySync()
	if requestingFullHistory {
		configureFullHistorySync()
		logger.Infof("Fresh pairing will request the maximum linked-device history WhatsApp permits")
	}

	// Create client instance
	client := whatsmeow.NewClient(deviceStore, logger)
	if client == nil {
		logger.Errorf("Failed to create WhatsApp client")
		return
	}

	// Initialize message store
	messageStore, err := NewMessageStore()
	if err != nil {
		logger.Errorf("Failed to initialize Convex message store: %v", err)
		return
	}
	defer messageStore.Close()

	// Setup event handling for messages and history sync. Initial connection
	// happens before the REST API is ready, so reconciliation is gated until
	// the CLI can call back into the bridge.
	restReady := &atomic.Bool{}
	fullHistoryComplete := make(chan struct{}, 1)
	client.AddEventHandler(func(evt interface{}) {
		switch v := evt.(type) {
		case *events.Message:
			// Process regular messages
			handleMessage(client, messageStore, v, logger)

		case *events.Receipt:
			handleReceipt(messageStore, v, logger)

		case *events.HistorySync:
			// Process history sync events
			handleHistorySync(client, messageStore, v, logger)
			if fullHistorySyncCompleted(v) {
				select {
				case fullHistoryComplete <- struct{}{}:
				default:
				}
			}

		case *events.Connected:
			logger.Infof("Connected to WhatsApp")
			if restReady.Load() {
				runMediaReconcileHook(logger)
			}

		case *events.LoggedOut:
			logger.Warnf("Device logged out, please relink WhatsApp")
		}
	})

	// Create channel to track connection success
	connected := make(chan bool, 1)

	// Connect to WhatsApp
	if client.Store.ID == nil {
		// No ID stored, this is a new client, need to pair with phone
		qrContext, cancelQR := context.WithCancel(context.Background())
		defer cancelQR()
		qrChan, _ := client.GetQRChannel(qrContext)
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}

		// QR pairing is the default path. When a phone number is configured
		// explicitly, use WhatsApp's code-pairing flow as the fallback path.
		pairPhone := getPairPhoneNumber()
		pairCodeRequested := pairPhone != ""
		pairCodeGenerated := false
		qrFallbackNoticePrinted := false
		for evt := range qrChan {
			if evt.Event == "code" {
				saveQRCodeArtifacts(evt.Code)
				if pairCodeRequested && !pairCodeGenerated {
					pairCodeGenerated = true
					code, err := client.PairPhone(
						context.Background(),
						pairPhone,
						true,
						whatsmeow.PairClientChrome,
						getPairPhoneDisplayName(),
					)
					if err != nil {
						logger.Errorf("Failed to generate phone pairing code: %v", err)
					} else {
						fmt.Printf("\nPairing code: %s\n", code)
						fmt.Println("In WhatsApp, use Linked devices -> Link with phone number instead, then enter this code.")
					}
				} else if !pairCodeRequested {
					fmt.Println("\nScan this QR code with your WhatsApp app:")
					qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
				} else if !qrFallbackNoticePrinted {
					qrFallbackNoticePrinted = true
					fmt.Println("\nWaiting for phone-number pairing. QR artifacts were also refreshed.")
				}
			} else if evt.Event == "success" {
				connected <- true
				break
			} else if evt.Event == "error" {
				logger.Errorf("Pairing error: %v", evt.Error)
			}
		}

		// Wait for connection
		select {
		case <-connected:
			fmt.Println("\nSuccessfully connected and authenticated!")
		case <-time.After(3 * time.Minute):
			logger.Errorf("Timeout waiting for WhatsApp pairing confirmation")
			return
		}
	} else {
		// Already logged in, just connect
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}
		connected <- true
	}

	// Wait a moment for connection to stabilize
	time.Sleep(2 * time.Second)

	if !client.IsConnected() {
		logger.Errorf("Failed to establish stable connection")
		return
	}
	syncIdentity(client, messageStore, logger)
	refreshFallbackDirectChatNames(client, messageStore, logger)

	fmt.Println("\n✓ Connected to WhatsApp! Type 'help' for commands.")

	// Start REST API server
	startRESTServer(client, messageStore, getHTTPPort(), logger)
	restReady.Store(true)
	runMediaReconcileHook(logger)

	if shouldExitAfterAuth() {
		if requestingFullHistory {
			timeout := getInitialHistorySyncTimeout()
			fmt.Printf("Setup mode active. Waiting up to %s for WhatsApp's full linked-device history stream...\n", timeout)
			select {
			case <-fullHistoryComplete:
				gracePeriod := getInitialHistorySyncGracePeriod()
				fmt.Printf("Full history stream reached 100%%. Waiting %s for final companion data...\n", gracePeriod)
				time.Sleep(gracePeriod)
			case <-time.After(timeout):
				logger.Warnf("WhatsApp did not signal completion of a full history stream within %s; preserving every history chunk received", timeout)
			}
		} else {
			gracePeriod := getInitialHistorySyncGracePeriod()
			fmt.Printf("Setup mode active. Waiting %s for final companion data...\n", gracePeriod)
			time.Sleep(gracePeriod)
		}
		fmt.Println("Setup complete. Disconnecting...")
		client.Disconnect()
		return
	}

	// Create a channel to keep the main goroutine alive
	exitChan := make(chan os.Signal, 1)
	signal.Notify(exitChan, syscall.SIGINT, syscall.SIGTERM)

	fmt.Println("REST server is running. Press Ctrl+C to disconnect and exit.")

	// Wait for termination signal
	<-exitChan

	fmt.Println("Disconnecting...")
	// Disconnect client
	client.Disconnect()
}

type directChatNameHints struct {
	Sender       string
	BusinessName string
	PushName     string
}

func directChatNameHintsFromMessage(info types.MessageInfo) directChatNameHints {
	hints := directChatNameHints{
		Sender:   info.Sender.User,
		PushName: info.PushName,
	}
	if info.VerifiedName != nil && info.VerifiedName.Details != nil {
		hints.BusinessName = info.VerifiedName.Details.GetVerifiedName()
	}
	return hints
}

func isNumericChatName(name string) bool {
	name = strings.TrimSpace(name)
	if name == "" {
		return false
	}

	digits := 0
	for _, char := range name {
		switch {
		case char >= '0' && char <= '9':
			digits++
		case char == '+', char == '-', char == ' ', char == '(', char == ')', char == '.', char == '@':
			continue
		default:
			return false
		}
	}
	return digits >= 6
}

func isFallbackDirectChatName(name string, jid types.JID, sender string) bool {
	name = strings.TrimSpace(name)
	if name == "" {
		return true
	}
	for _, fallback := range []string{jid.User, jid.String(), sender} {
		if fallback != "" && name == fallback {
			return true
		}
	}
	return isNumericChatName(name)
}

func resolveDirectChatName(existingName string, jid types.JID, contact types.ContactInfo, hints directChatNameHints) string {
	existingName = strings.TrimSpace(existingName)
	if existingName != "" && !isFallbackDirectChatName(existingName, jid, hints.Sender) {
		return existingName
	}

	// A saved address-book name remains authoritative. The verified name on the
	// current event comes next because whatsmeow updates its cached contact names
	// asynchronously after dispatching the message event.
	for _, candidate := range []string{
		contact.FullName,
		hints.BusinessName,
		contact.BusinessName,
		hints.PushName,
		contact.PushName,
		contact.FirstName,
	} {
		if candidate = strings.TrimSpace(candidate); candidate != "" {
			return candidate
		}
	}

	if existingName != "" {
		return existingName
	}
	if sender := strings.TrimSpace(hints.Sender); sender != "" {
		return sender
	}
	return jid.User
}

// refreshFallbackDirectChatNames repairs numeric names left behind by older
// bridge versions. It consults only whatsmeow's existing local contact cache and
// never replaces a meaningful saved label.
func refreshFallbackDirectChatNames(client *whatsmeow.Client, messageStore *MessageStore, logger waLog.Logger) {
	if client == nil || client.Store == nil || client.Store.Contacts == nil || messageStore == nil {
		return
	}

	chats, err := messageStore.GetChats()
	if err != nil {
		logger.Warnf("Failed to list chats for name refresh: %v", err)
		return
	}

	refreshed := 0
	for chatJID, lastMessageTime := range chats {
		jid, err := types.ParseJID(chatJID)
		if err != nil || jid.Server == types.GroupServer {
			continue
		}

		existingName, err := messageStore.ChatName(chatJID)
		if err != nil {
			continue
		}
		if !isFallbackDirectChatName(existingName, jid, jid.User) {
			continue
		}

		contact, err := client.Store.Contacts.GetContact(context.Background(), jid)
		if err != nil {
			continue
		}
		name := resolveDirectChatName(existingName, jid, contact, directChatNameHints{Sender: jid.User})
		if name == existingName {
			continue
		}
		if err := messageStore.StoreChat(chatJID, name, lastMessageTime); err != nil {
			logger.Warnf("Failed to refresh chat name for %s: %v", chatJID, err)
			continue
		}
		refreshed++
	}

	if refreshed > 0 {
		logger.Infof("Refreshed %d fallback chat names from the local contact cache", refreshed)
	}
}

// GetChatName determines the appropriate name for a chat based on JID and other info.
func GetChatName(client *whatsmeow.Client, messageStore *MessageStore, jid types.JID, chatJID string, conversation interface{}, hints directChatNameHints, logger waLog.Logger) string {
	// First, check if the chat is already stored with a name
	existingName, err := messageStore.ChatName(chatJID)
	if err == nil && existingName != "" && (jid.Server == "g.us" || !isFallbackDirectChatName(existingName, jid, hints.Sender)) {
		// Chat exists with a name, use that
		logger.Infof("Using existing chat name for %s: %s", chatJID, existingName)
		return existingName
	}

	// Need to determine chat name
	var name string

	if jid.Server == "g.us" {
		// This is a group chat
		logger.Infof("Getting name for group: %s", chatJID)

		// Use conversation data if provided (from history sync)
		if conversation != nil {
			// Extract name from conversation if available
			// This uses type assertions to handle different possible types
			var displayName, convName *string
			// Try to extract the fields we care about regardless of the exact type
			v := reflect.ValueOf(conversation)
			if v.Kind() == reflect.Ptr && !v.IsNil() {
				v = v.Elem()

				// Try to find DisplayName field
				if displayNameField := v.FieldByName("DisplayName"); displayNameField.IsValid() && displayNameField.Kind() == reflect.Ptr && !displayNameField.IsNil() {
					dn := displayNameField.Elem().String()
					displayName = &dn
				}

				// Try to find Name field
				if nameField := v.FieldByName("Name"); nameField.IsValid() && nameField.Kind() == reflect.Ptr && !nameField.IsNil() {
					n := nameField.Elem().String()
					convName = &n
				}
			}

			// Use the name we found
			if displayName != nil && *displayName != "" {
				name = *displayName
			} else if convName != nil && *convName != "" {
				name = *convName
			}
		}

		// If we didn't get a name, try group info
		if name == "" {
			groupInfo, err := client.GetGroupInfo(context.Background(), jid)
			if err == nil && groupInfo.Name != "" {
				name = groupInfo.Name
			} else {
				// Fallback name for groups
				name = fmt.Sprintf("Group %s", jid.User)
			}
		}

		logger.Infof("Using group name: %s", name)
	} else {
		// This is an individual contact
		logger.Infof("Getting name for contact: %s", chatJID)

		contact := types.ContactInfo{}
		if client != nil && client.Store != nil && client.Store.Contacts != nil {
			if storedContact, err := client.Store.Contacts.GetContact(context.Background(), jid); err == nil {
				contact = storedContact
			}
		}
		name = resolveDirectChatName(existingName, jid, contact, hints)

		logger.Infof("Using contact name: %s", name)
	}

	return name
}

// Handle history sync events
func handleHistorySync(client *whatsmeow.Client, messageStore *MessageStore, historySync *events.HistorySync, logger waLog.Logger) {
	if historySync == nil || historySync.Data == nil {
		logger.Warnf("Received an empty history sync event")
		return
	}

	fmt.Printf(
		"Received %s history sync event at %d%% with %d conversations\n",
		historySync.Data.GetSyncType(),
		historySync.Data.GetProgress(),
		len(historySync.Data.Conversations),
	)

	syncedCount := 0
	for _, conversation := range historySync.Data.Conversations {
		// Parse JID from the conversation
		if conversation.ID == nil {
			continue
		}

		chatJID := *conversation.ID

		// Try to parse the JID
		jid, err := types.ParseJID(chatJID)
		if err != nil {
			logger.Warnf("Failed to parse JID %s: %v", chatJID, err)
			continue
		}

		// Get appropriate chat name by passing the history sync conversation directly
		name := GetChatName(client, messageStore, jid, chatJID, conversation, directChatNameHints{}, logger)

		// Process messages
		messages := conversation.Messages
		if len(messages) > 0 {
			// Update chat with latest message timestamp
			latestMsg := messages[0]
			if latestMsg == nil || latestMsg.Message == nil {
				continue
			}

			// Get timestamp from message info
			timestamp := time.Time{}
			if ts := latestMsg.Message.GetMessageTimestamp(); ts != 0 {
				timestamp = time.Unix(int64(ts), 0)
			} else {
				continue
			}

			messageStore.StoreChat(chatJID, name, timestamp)

			// Store messages
			for _, msg := range messages {
				if msg == nil || msg.Message == nil {
					continue
				}

				parsedMessage, err := client.ParseWebMessage(jid, msg.Message)
				if err != nil {
					logger.Warnf("Failed to parse history message in %s: %v", chatJID, err)
					continue
				}

				err = storeEventMessage(client, messageStore, parsedMessage, logger)
				if err != nil {
					logger.Warnf("Failed to store history message: %v", err)
				} else {
					syncedCount++
					if shouldLogMessageContent() {
						messageID, normalizedMessage, _ := normalizeMessageForStorage(parsedMessage)
						content := extractTextContent(normalizedMessage)
						mediaType, filename, _, _, _, _, _ := extractMediaInfo(normalizedMessage, messageID)

						// Log successful message storage
						if mediaType != "" {
							logger.Infof("Stored message: [%s] %s -> %s: [%s: %s] %s",
								parsedMessage.Info.Timestamp.Format("2006-01-02 15:04:05"), parsedMessage.Info.Sender.User, chatJID, mediaType, filename, content)
						} else {
							logger.Infof("Stored message: [%s] %s -> %s: %s",
								parsedMessage.Info.Timestamp.Format("2006-01-02 15:04:05"), parsedMessage.Info.Sender.User, chatJID, content)
						}
					}
				}
			}
		}
	}

	fmt.Printf("History sync complete. Stored %d messages.\n", syncedCount)
	syncIdentity(client, messageStore, logger)
	runMediaReconcileHook(logger)
}

// Request history sync from the server
func requestHistorySync(client *whatsmeow.Client) {
	if client == nil {
		fmt.Println("Client is not initialized. Cannot request history sync.")
		return
	}

	if !client.IsConnected() {
		fmt.Println("Client is not connected. Please ensure you are connected to WhatsApp first.")
		return
	}

	if client.Store.ID == nil {
		fmt.Println("Client is not logged in. Please scan the QR code first.")
		return
	}

	// Build and send a history sync request
	historyMsg := client.BuildHistorySyncRequest(nil, 100)
	if historyMsg == nil {
		fmt.Println("Failed to build history sync request.")
		return
	}

	_, err := client.SendMessage(context.Background(), types.JID{
		Server: "s.whatsapp.net",
		User:   "status",
	}, historyMsg)

	if err != nil {
		fmt.Printf("Failed to request history sync: %v\n", err)
	} else {
		fmt.Println("History sync requested. Waiting for server response...")
	}
}

// analyzeOggOpus tries to extract duration and generate a simple waveform from an Ogg Opus file
func analyzeOggOpus(data []byte) (duration uint32, waveform []byte, err error) {
	// Try to detect if this is a valid Ogg file by checking for the "OggS" signature
	// at the beginning of the file
	if len(data) < 4 || string(data[0:4]) != "OggS" {
		return 0, nil, fmt.Errorf("not a valid Ogg file (missing OggS signature)")
	}

	// Parse Ogg pages to find the last page with a valid granule position
	var lastGranule uint64
	var sampleRate uint32 = 48000 // Default Opus sample rate
	var preSkip uint16 = 0
	var foundOpusHead bool

	// Scan through the file looking for Ogg pages
	for i := 0; i < len(data); {
		// Check if we have enough data to read Ogg page header
		if i+27 >= len(data) {
			break
		}

		// Verify Ogg page signature
		if string(data[i:i+4]) != "OggS" {
			// Skip until next potential page
			i++
			continue
		}

		// Extract header fields
		granulePos := binary.LittleEndian.Uint64(data[i+6 : i+14])
		pageSeqNum := binary.LittleEndian.Uint32(data[i+18 : i+22])
		numSegments := int(data[i+26])

		// Extract segment table
		if i+27+numSegments >= len(data) {
			break
		}
		segmentTable := data[i+27 : i+27+numSegments]

		// Calculate page size
		pageSize := 27 + numSegments
		for _, segLen := range segmentTable {
			pageSize += int(segLen)
		}

		// Check if we're looking at an OpusHead packet (should be in first few pages)
		if !foundOpusHead && pageSeqNum <= 1 {
			// Look for "OpusHead" marker in this page
			pageData := data[i : i+pageSize]
			headPos := bytes.Index(pageData, []byte("OpusHead"))
			if headPos >= 0 && headPos+12 < len(pageData) {
				// Found OpusHead, extract sample rate and pre-skip
				// OpusHead format: Magic(8) + Version(1) + Channels(1) + PreSkip(2) + SampleRate(4) + ...
				headPos += 8 // Skip "OpusHead" marker
				// PreSkip is 2 bytes at offset 10
				if headPos+12 <= len(pageData) {
					preSkip = binary.LittleEndian.Uint16(pageData[headPos+10 : headPos+12])
					sampleRate = binary.LittleEndian.Uint32(pageData[headPos+12 : headPos+16])
					foundOpusHead = true
					fmt.Printf("Found OpusHead: sampleRate=%d, preSkip=%d\n", sampleRate, preSkip)
				}
			}
		}

		// Keep track of last valid granule position
		if granulePos != 0 {
			lastGranule = granulePos
		}

		// Move to next page
		i += pageSize
	}

	if !foundOpusHead {
		fmt.Println("Warning: OpusHead not found, using default values")
	}

	// Calculate duration based on granule position
	if lastGranule > 0 {
		// Formula for duration: (lastGranule - preSkip) / sampleRate
		durationSeconds := float64(lastGranule-uint64(preSkip)) / float64(sampleRate)
		duration = uint32(math.Ceil(durationSeconds))
		fmt.Printf("Calculated Opus duration from granule: %f seconds (lastGranule=%d)\n",
			durationSeconds, lastGranule)
	} else {
		// Fallback to rough estimation if granule position not found
		fmt.Println("Warning: No valid granule position found, using estimation")
		durationEstimate := float64(len(data)) / 2000.0 // Very rough approximation
		duration = uint32(durationEstimate)
	}

	// Make sure we have a reasonable duration (at least 1 second, at most 300 seconds)
	if duration < 1 {
		duration = 1
	} else if duration > 300 {
		duration = 300
	}

	// Generate waveform
	waveform = placeholderWaveform(duration)

	fmt.Printf("Ogg Opus analysis: size=%d bytes, calculated duration=%d sec, waveform=%d bytes\n",
		len(data), duration, len(waveform))

	return duration, waveform, nil
}

// min returns the smaller of x or y
func min(x, y int) int {
	if x < y {
		return x
	}
	return y
}

// placeholderWaveform generates a synthetic waveform for WhatsApp voice messages
// that appears natural with some variability based on the duration
func placeholderWaveform(duration uint32) []byte {
	// WhatsApp expects a 64-byte waveform for voice messages
	const waveformLength = 64
	waveform := make([]byte, waveformLength)

	// Seed the random number generator for consistent results with the same duration
	rand.Seed(int64(duration))

	// Create a more natural looking waveform with some patterns and variability
	// rather than completely random values

	// Base amplitude and frequency - longer messages get faster frequency
	baseAmplitude := 35.0
	frequencyFactor := float64(min(int(duration), 120)) / 30.0

	for i := range waveform {
		// Position in the waveform (normalized 0-1)
		pos := float64(i) / float64(waveformLength)

		// Create a wave pattern with some randomness
		// Use multiple sine waves of different frequencies for more natural look
		val := baseAmplitude * math.Sin(pos*math.Pi*frequencyFactor*8)
		val += (baseAmplitude / 2) * math.Sin(pos*math.Pi*frequencyFactor*16)

		// Add some randomness to make it look more natural
		val += (rand.Float64() - 0.5) * 15

		// Add some fade-in and fade-out effects
		fadeInOut := math.Sin(pos * math.Pi)
		val = val * (0.7 + 0.3*fadeInOut)

		// Center around 50 (typical voice baseline)
		val = val + 50

		// Ensure values stay within WhatsApp's expected range (0-100)
		if val < 0 {
			val = 0
		} else if val > 100 {
			val = 100
		}

		waveform[i] = byte(val)
	}

	return waveform
}
