package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	waCompanionReg "go.mau.fi/whatsmeow/proto/waCompanionReg"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	waStore "go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

func TestConfigureFullHistorySyncRequestsFullHistory(t *testing.T) {
	originalDeviceProps := proto.Clone(waStore.DeviceProps).(*waCompanionReg.DeviceProps)
	defer func() {
		waStore.DeviceProps = originalDeviceProps
	}()

	configureFullHistorySync()

	if !waStore.DeviceProps.GetRequireFullSync() {
		t.Fatal("RequireFullSync = false, want true")
	}
}

func TestFullHistorySyncCompletedRequiresFullAtOneHundredPercent(t *testing.T) {
	fullNinetyNine := &events.HistorySync{Data: &waHistorySync.HistorySync{
		SyncType: waHistorySync.HistorySync_FULL.Enum(),
		Progress: proto.Uint32(99),
	}}
	fullComplete := &events.HistorySync{Data: &waHistorySync.HistorySync{
		SyncType: waHistorySync.HistorySync_FULL.Enum(),
		Progress: proto.Uint32(100),
	}}
	recentComplete := &events.HistorySync{Data: &waHistorySync.HistorySync{
		SyncType: waHistorySync.HistorySync_RECENT.Enum(),
		Progress: proto.Uint32(100),
	}}

	if fullHistorySyncCompleted(nil) || fullHistorySyncCompleted(&events.HistorySync{}) {
		t.Fatal("empty history sync was treated as complete")
	}
	if fullHistorySyncCompleted(fullNinetyNine) {
		t.Fatal("99% full history sync was treated as complete")
	}
	if fullHistorySyncCompleted(recentComplete) {
		t.Fatal("recent history sync was treated as full history completion")
	}
	if !fullHistorySyncCompleted(fullComplete) {
		t.Fatal("100% full history sync was not treated as complete")
	}
}

func TestSenderToParticipantResolvesBareStoredLID(t *testing.T) {
	storeDir := t.TempDir()
	t.Setenv("WHATSAPP_MCP_STORE_DIR", storeDir)

	db, err := sql.Open("sqlite3", filepath.Join(storeDir, "whatsapp.db"))
	if err != nil {
		t.Fatalf("sql.Open() error = %v", err)
	}
	defer db.Close()

	if _, err := db.Exec("CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE NOT NULL)"); err != nil {
		t.Fatalf("CREATE TABLE error = %v", err)
	}
	if _, err := db.Exec("INSERT INTO whatsmeow_lid_map (lid, pn) VALUES (?, ?)", "99900123456789", "15551230001"); err != nil {
		t.Fatalf("INSERT error = %v", err)
	}

	if got := senderToParticipant("99900123456789"); got != "99900123456789@lid" {
		t.Fatalf("senderToParticipant() = %q, want LID participant", got)
	}
}

func TestSenderToParticipantFallsBackToPhoneJID(t *testing.T) {
	t.Setenv("WHATSAPP_MCP_STORE_DIR", t.TempDir())

	if got := senderToParticipant("15551230001"); got != "15551230001@s.whatsapp.net" {
		t.Fatalf("senderToParticipant() = %q, want phone participant", got)
	}
}

func TestMessageSenderJIDPreservesGroupLIDParticipant(t *testing.T) {
	sender := types.NewJID("99900123456789", types.HiddenUserServer)
	chat := types.NewJID("120363424447729748", types.GroupServer)

	if got := messageSenderJID(sender, chat); got != "99900123456789@lid" {
		t.Fatalf("messageSenderJID() = %q, want full LID JID", got)
	}
}

func TestMessageSenderJIDFallsBackToDirectChatJID(t *testing.T) {
	chat := types.NewJID("15551230001", types.DefaultUserServer)

	if got := messageSenderJID(types.JID{}, chat); got != "15551230001@s.whatsapp.net" {
		t.Fatalf("messageSenderJID() = %q, want direct chat JID", got)
	}
}

func TestExtractReactionMetadataReadsPlainReaction(t *testing.T) {
	timestamp := time.Unix(1_700_000_000, 0)
	evt := &events.Message{
		Info: types.MessageInfo{
			MessageSource: types.MessageSource{
				Chat:   types.NewJID("120363424447729748", types.GroupServer),
				Sender: types.NewJID("15551230001", types.DefaultUserServer),
			},
			ID:        types.MessageID("REACTION-ID"),
			Timestamp: timestamp,
		},
		Message: &waProto.Message{
			ReactionMessage: &waProto.ReactionMessage{
				Key: &waProto.MessageKey{
					RemoteJID:   proto.String("120363424447729748@g.us"),
					ID:          proto.String("TARGET-ID"),
					Participant: proto.String("15550987654@s.whatsapp.net"),
				},
				Text:              proto.String("+1"),
				GroupingKey:       proto.String("grouping-key"),
				SenderTimestampMS: proto.Int64(1_700_000_001_000),
			},
		},
	}

	reaction, ok := extractReactionMetadata(nil, evt, "REACTION-ID", evt.Message, nil)
	if !ok {
		t.Fatal("extractReactionMetadata() did not detect reaction")
	}
	if reaction.ChatJID != "120363424447729748@g.us" {
		t.Fatalf("ChatJID = %q", reaction.ChatJID)
	}
	if reaction.TargetMessageID != "TARGET-ID" {
		t.Fatalf("TargetMessageID = %q", reaction.TargetMessageID)
	}
	if reaction.TargetSender != "15550987654@s.whatsapp.net" {
		t.Fatalf("TargetSender = %q", reaction.TargetSender)
	}
	if reaction.Sender != "15551230001@s.whatsapp.net" {
		t.Fatalf("Sender = %q", reaction.Sender)
	}
	if reaction.Emoji != "+1" {
		t.Fatalf("Emoji = %q", reaction.Emoji)
	}
	if reaction.GroupingKey != "grouping-key" {
		t.Fatalf("GroupingKey = %q", reaction.GroupingKey)
	}
	if !reaction.Timestamp.Equal(timestamp) {
		t.Fatalf("Timestamp = %s, want %s", reaction.Timestamp, timestamp)
	}
}

func TestStoreReactionUpsertsAndRemovesCurrentReaction(t *testing.T) {
	t.Setenv("WHATSAPP_MCP_STORE_DIR", t.TempDir())

	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore() error = %v", err)
	}
	defer store.Close()

	reaction := ReactionMetadata{
		ReactionMessageID: "REACTION-ID",
		ChatJID:           "chat@g.us",
		TargetMessageID:   "TARGET-ID",
		Sender:            "15551230001@s.whatsapp.net",
		Emoji:             "+1",
		Timestamp:         time.Unix(1_700_000_000, 0),
	}
	if err := store.StoreReaction(reaction); err != nil {
		t.Fatalf("StoreReaction() error = %v", err)
	}

	reaction.ReactionMessageID = "REACTION-ID-2"
	reaction.Emoji = "ok"
	if err := store.StoreReaction(reaction); err != nil {
		t.Fatalf("StoreReaction() update error = %v", err)
	}

	var emoji string
	var count int
	if err := store.db.QueryRow("SELECT emoji, COUNT(*) FROM message_reactions WHERE chat_jid = ? AND target_message_id = ?", "chat@g.us", "TARGET-ID").Scan(&emoji, &count); err != nil {
		t.Fatalf("SELECT reaction error = %v", err)
	}
	if emoji != "ok" || count != 1 {
		t.Fatalf("reaction row = (%q, %d), want (ok, 1)", emoji, count)
	}

	reaction.Emoji = ""
	if err := store.StoreReaction(reaction); err != nil {
		t.Fatalf("StoreReaction() remove error = %v", err)
	}
	if err := store.db.QueryRow("SELECT COUNT(*) FROM message_reactions").Scan(&count); err != nil {
		t.Fatalf("COUNT reactions error = %v", err)
	}
	if count != 0 {
		t.Fatalf("reaction count = %d, want 0", count)
	}
}

func TestStoreReceiptPersistsReadReceipt(t *testing.T) {
	t.Setenv("WHATSAPP_MCP_STORE_DIR", t.TempDir())

	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore() error = %v", err)
	}
	defer store.Close()

	timestamp := time.Unix(1_700_000_000, 0)
	if err := store.StoreReceipt(
		"MSG-ID",
		"15550987654@s.whatsapp.net",
		normalizeReceiptType(types.ReceiptTypeRead),
		"15550987654@s.whatsapp.net",
		"me",
		timestamp,
	); err != nil {
		t.Fatalf("StoreReceipt() error = %v", err)
	}

	var receiptType, receiptSender, messageSender string
	if err := store.db.QueryRow("SELECT receipt_type, receipt_sender, message_sender FROM message_receipts WHERE message_id = ?", "MSG-ID").Scan(&receiptType, &receiptSender, &messageSender); err != nil {
		t.Fatalf("SELECT receipt error = %v", err)
	}
	if receiptType != "read" || receiptSender != "15550987654@s.whatsapp.net" || messageSender != "me" {
		t.Fatalf("receipt row = (%q, %q, %q)", receiptType, receiptSender, messageSender)
	}
}

func TestBuildReplyContextPreservesFullParticipantJID(t *testing.T) {
	contextInfo := buildReplyContext(ReplyMetadata{
		MessageID: "3AEE6D27A072B2867813",
		Sender:    "99900123456789@lid",
		Content:   "quoted text",
	})

	if contextInfo == nil {
		t.Fatal("buildReplyContext() returned nil")
	}
	if got := contextInfo.GetParticipant(); got != "99900123456789@lid" {
		t.Fatalf("Participant = %q, want full LID JID", got)
	}
	if got := contextInfo.GetStanzaID(); got != "3AEE6D27A072B2867813" {
		t.Fatalf("StanzaID = %q", got)
	}
}

func TestExtractTextContentReadsMediaCaptions(t *testing.T) {
	tests := []struct {
		name string
		msg  *waProto.Message
		want string
	}{
		{
			name: "conversation text",
			msg: &waProto.Message{
				Conversation: proto.String("plain text"),
			},
			want: "plain text",
		},
		{
			name: "extended text",
			msg: &waProto.Message{
				ExtendedTextMessage: &waProto.ExtendedTextMessage{
					Text: proto.String("rich text"),
				},
			},
			want: "rich text",
		},
		{
			name: "image caption",
			msg: &waProto.Message{
				ImageMessage: &waProto.ImageMessage{
					Caption: proto.String("image caption"),
				},
			},
			want: "image caption",
		},
		{
			name: "video caption",
			msg: &waProto.Message{
				VideoMessage: &waProto.VideoMessage{
					Caption: proto.String("video caption"),
				},
			},
			want: "video caption",
		},
		{
			name: "document caption",
			msg: &waProto.Message{
				DocumentMessage: &waProto.DocumentMessage{
					Caption: proto.String("document caption"),
				},
			},
			want: "document caption",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := extractTextContent(tt.msg); got != tt.want {
				t.Fatalf("extractTextContent() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestExtractTextContentReadsBusinessMessages(t *testing.T) {
	tests := []struct {
		name string
		msg  *waProto.Message
		want string
	}{
		{
			name: "hydrated template",
			msg: &waProto.Message{
				TemplateMessage: &waProto.TemplateMessage{
					HydratedTemplate: &waProto.TemplateMessage_HydratedFourRowTemplate{
						Title: &waProto.TemplateMessage_HydratedFourRowTemplate_HydratedTitleText{
							HydratedTitleText: "Delivery update",
						},
						HydratedContentText: proto.String("Your order is at the door."),
						HydratedFooterText:  proto.String("Delivery service"),
					},
				},
			},
			want: "Delivery update\nYour order is at the door.\nDelivery service",
		},
		{
			name: "list message",
			msg: &waProto.Message{
				ListMessage: &waProto.ListMessage{
					Title:       proto.String("Choose a service"),
					Description: proto.String("Select the option you need."),
					ButtonText:  proto.String("View options"),
					FooterText:  proto.String("Support"),
					Sections: []*waProto.ListMessage_Section{
						{
							Title: proto.String("Internet"),
							Rows: []*waProto.ListMessage_Row{
								{
									Title:       proto.String("Report an outage"),
									Description: proto.String("Check your connection."),
									RowID:       proto.String("internal-routing-id"),
								},
							},
						},
					},
				},
			},
			want: "Choose a service\nSelect the option you need.\nView options\nSupport\nInternet\nReport an outage\nCheck your connection.",
		},
		{
			name: "interactive message",
			msg: &waProto.Message{
				InteractiveMessage: &waProto.InteractiveMessage{
					Header: &waProto.InteractiveMessage_Header{
						Title:    proto.String("Account alert"),
						Subtitle: proto.String("New login"),
					},
					Body:   &waProto.InteractiveMessage_Body{Text: proto.String("A new device signed in.")},
					Footer: &waProto.InteractiveMessage_Footer{Text: proto.String("Security")},
				},
			},
			want: "Account alert\nNew login\nA new device signed in.\nSecurity",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := extractTextContent(tt.msg); got != tt.want {
				t.Fatalf("extractTextContent() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestStoreEventMessagePersistsTemplateText(t *testing.T) {
	t.Setenv("WHATSAPP_MCP_STORE_DIR", t.TempDir())

	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore() error = %v", err)
	}
	defer store.Close()

	evt := &events.Message{
		Info: types.MessageInfo{
			MessageSource: types.MessageSource{
				Chat:   types.NewJID("15551230001", types.DefaultUserServer),
				Sender: types.NewJID("15551230001", types.DefaultUserServer),
			},
			ID:        types.MessageID("BUSINESS-TEMPLATE-ID"),
			Timestamp: time.Unix(1_700_000_000, 0),
		},
		Message: &waProto.Message{
			TemplateMessage: &waProto.TemplateMessage{
				HydratedTemplate: &waProto.TemplateMessage_HydratedFourRowTemplate{
					HydratedContentText: proto.String("Your appointment is confirmed."),
				},
			},
		},
	}
	if err := store.StoreChat(evt.Info.Chat.String(), "Example Business", evt.Info.Timestamp); err != nil {
		t.Fatalf("StoreChat() error = %v", err)
	}

	if err := storeEventMessage(nil, store, evt, nil); err != nil {
		t.Fatalf("storeEventMessage() error = %v", err)
	}

	var content string
	if err := store.db.QueryRow("SELECT content FROM messages WHERE id = ?", "BUSINESS-TEMPLATE-ID").Scan(&content); err != nil {
		t.Fatalf("SELECT stored template error = %v", err)
	}
	if content != "Your appointment is confirmed." {
		t.Fatalf("stored content = %q", content)
	}
}

func TestResolveDirectChatNameRefreshesNumericFallback(t *testing.T) {
	jid := types.NewJID("15551230001", types.DefaultUserServer)
	contact := types.ContactInfo{BusinessName: "Example Business"}
	hints := directChatNameHints{Sender: jid.User}

	if got := resolveDirectChatName(jid.User, jid, contact, hints); got != "Example Business" {
		t.Fatalf("resolveDirectChatName() = %q, want verified business name", got)
	}
}

func TestResolveDirectChatNamePreservesMeaningfulExistingName(t *testing.T) {
	jid := types.NewJID("15551230001", types.DefaultUserServer)
	contact := types.ContactInfo{BusinessName: "Example Business"}
	hints := directChatNameHints{Sender: jid.User, BusinessName: "New Business Name"}

	if got := resolveDirectChatName("My saved label", jid, contact, hints); got != "My saved label" {
		t.Fatalf("resolveDirectChatName() = %q, want existing meaningful name", got)
	}
}

func TestResolveDirectChatNameUsesCurrentVerifiedNameBeforeCachedBusinessName(t *testing.T) {
	jid := types.NewJID("15551230001", types.DefaultUserServer)
	contact := types.ContactInfo{BusinessName: "Old Business Name"}
	hints := directChatNameHints{Sender: jid.User, BusinessName: "Current Business Name"}

	if got := resolveDirectChatName(jid.User, jid, contact, hints); got != "Current Business Name" {
		t.Fatalf("resolveDirectChatName() = %q, want current verified business name", got)
	}
}

func TestRefreshFallbackDirectChatNamesUsesWhatsmeowContactCache(t *testing.T) {
	ctx := context.Background()
	sessionPath := filepath.Join(t.TempDir(), "session.db")
	container, err := sqlstore.New(ctx, "sqlite3", "file:"+sessionPath+"?_foreign_keys=on", waLog.Noop)
	if err != nil {
		t.Fatalf("sqlstore.New() error = %v", err)
	}
	defer container.Close()

	device := container.NewDevice()
	ownJID := types.NewJID("15550000000", types.DefaultUserServer)
	device.ID = &ownJID
	device.Account = &waProto.ADVSignedDeviceIdentity{
		Details:             []byte{},
		AccountSignatureKey: make([]byte, 32),
		AccountSignature:    make([]byte, 64),
		DeviceSignature:     make([]byte, 64),
	}
	if err := device.Save(ctx); err != nil {
		t.Fatalf("device.Save() error = %v", err)
	}

	chatJID := types.NewJID("15551230001", types.DefaultUserServer)
	if _, _, err := device.Contacts.PutBusinessName(ctx, chatJID, "Example Business"); err != nil {
		t.Fatalf("PutBusinessName() error = %v", err)
	}

	t.Setenv("WHATSAPP_MCP_STORE_DIR", t.TempDir())
	messageStore, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore() error = %v", err)
	}
	defer messageStore.Close()

	timestamp := time.Unix(1_700_000_000, 0)
	if err := messageStore.StoreChat(chatJID.String(), chatJID.User, timestamp); err != nil {
		t.Fatalf("StoreChat() error = %v", err)
	}

	client := whatsmeow.NewClient(device, waLog.Noop)
	refreshFallbackDirectChatNames(client, messageStore, waLog.Noop)

	var name string
	if err := messageStore.db.QueryRow("SELECT name FROM chats WHERE jid = ?", chatJID.String()).Scan(&name); err != nil {
		t.Fatalf("SELECT refreshed chat error = %v", err)
	}
	if name != "Example Business" {
		t.Fatalf("refreshed chat name = %q, want business name", name)
	}
}

func TestResolveOutboundMediaType(t *testing.T) {
	tests := []struct {
		name          string
		path          string
		wantMediaType whatsmeow.MediaType
		wantMIMEType  string
	}{
		{
			name:          "PDF is an actual PDF document",
			path:          "/tmp/Guide.PDF",
			wantMediaType: whatsmeow.MediaDocument,
			wantMIMEType:  "application/pdf",
		},
		{
			name:          "JPEG remains an image",
			path:          "/tmp/photo.jpg",
			wantMediaType: whatsmeow.MediaImage,
			wantMIMEType:  "image/jpeg",
		},
		{
			name:          "unknown documents remain generic binary",
			path:          "/tmp/archive.unknown",
			wantMediaType: whatsmeow.MediaDocument,
			wantMIMEType:  "application/octet-stream",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			mediaType, mimeType := resolveOutboundMediaType(tt.path)
			if mediaType != tt.wantMediaType {
				t.Fatalf("mediaType = %v, want %v", mediaType, tt.wantMediaType)
			}
			if mimeType != tt.wantMIMEType {
				t.Fatalf("mimeType = %q, want %q", mimeType, tt.wantMIMEType)
			}
		})
	}
}

func TestBuildMediaFilenameUsesMessageID(t *testing.T) {
	tests := []struct {
		name         string
		mediaType    string
		messageID    string
		originalName string
		want         string
	}{
		{
			name:      "image filename",
			mediaType: "image",
			messageID: "3A7F7B003B26545A2A5C",
			want:      "image_3A7F7B003B26545A2A5C.jpg",
		},
		{
			name:      "video filename",
			mediaType: "video",
			messageID: "ABC123",
			want:      "video_ABC123.mp4",
		},
		{
			name:      "audio filename",
			mediaType: "audio",
			messageID: "ABC:123/456",
			want:      "audio_ABC_123_456.ogg",
		},
		{
			name:         "document keeps provided name",
			mediaType:    "document",
			messageID:    "DOC123",
			originalName: "invoice.pdf",
			want:         "invoice.pdf",
		},
		{
			name:      "document fallback uses message id",
			mediaType: "document",
			messageID: "DOC123",
			want:      "document_DOC123",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := buildMediaFilename(tt.mediaType, tt.messageID, tt.originalName); got != tt.want {
				t.Fatalf("buildMediaFilename() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestResolveDownloadFilenameUsesCanonicalMediaName(t *testing.T) {
	tests := []struct {
		name         string
		mediaType    string
		messageID    string
		storedName   string
		wantFilename string
	}{
		{
			name:         "image ignores stale stored name",
			mediaType:    "image",
			messageID:    "3AB93D8B95628CB5EEE9",
			storedName:   "image_20260330_165202.jpg",
			wantFilename: "image_3AB93D8B95628CB5EEE9.jpg",
		},
		{
			name:         "document keeps provided name",
			mediaType:    "document",
			messageID:    "DOC123",
			storedName:   "invoice.pdf",
			wantFilename: "invoice.pdf",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := resolveDownloadFilename(tt.mediaType, tt.messageID, tt.storedName); got != tt.wantFilename {
				t.Fatalf("resolveDownloadFilename() = %q, want %q", got, tt.wantFilename)
			}
		})
	}
}

func TestFileMatchesStoredSHA256(t *testing.T) {
	tmpDir := t.TempDir()
	filePath := filepath.Join(tmpDir, "sample.jpg")
	data := []byte("not really a jpeg, but enough for hashing")

	if err := os.WriteFile(filePath, data, 0o644); err != nil {
		t.Fatalf("os.WriteFile() error = %v", err)
	}

	expected := sha256.Sum256(data)
	matches, err := fileMatchesStoredSHA256(filePath, expected[:])
	if err != nil {
		t.Fatalf("fileMatchesStoredSHA256() unexpected error = %v", err)
	}
	if !matches {
		t.Fatalf("fileMatchesStoredSHA256() = false, want true")
	}

	wrong := sha256.Sum256([]byte("different"))
	matches, err = fileMatchesStoredSHA256(filePath, wrong[:])
	if err != nil {
		t.Fatalf("fileMatchesStoredSHA256() unexpected error = %v", err)
	}
	if matches {
		t.Fatalf("fileMatchesStoredSHA256() = true, want false")
	}
}

func TestNormalizeMessageForStorageUsesOriginalIDAndEditedPayload(t *testing.T) {
	editedPayload := &waProto.Message{
		ImageMessage: &waProto.ImageMessage{
			Caption: proto.String("edited caption"),
		},
	}

	evt := &events.Message{
		Info: types.MessageInfo{
			ID:        types.MessageID("EDIT-STANZA-ID"),
			Timestamp: time.Unix(1_700_000_000, 0),
		},
		IsEdit: true,
		Message: &waProto.Message{
			ProtocolMessage: &waProto.ProtocolMessage{
				Type: waProto.ProtocolMessage_MESSAGE_EDIT.Enum(),
				Key: &waProto.MessageKey{
					ID: proto.String("ORIGINAL-ID"),
				},
				EditedMessage: editedPayload,
			},
		},
	}

	gotID, gotMessage, gotIsEdit := normalizeMessageForStorage(evt)
	if !gotIsEdit {
		t.Fatalf("normalizeMessageForStorage() isEdit = false, want true")
	}
	if gotID != "ORIGINAL-ID" {
		t.Fatalf("normalizeMessageForStorage() id = %q, want %q", gotID, "ORIGINAL-ID")
	}
	if gotMessage != editedPayload {
		t.Fatalf("normalizeMessageForStorage() message payload was not rewritten to edited content")
	}
	if gotCaption := extractTextContent(gotMessage); gotCaption != "edited caption" {
		t.Fatalf("extractTextContent(normalized) = %q, want %q", gotCaption, "edited caption")
	}
}

func TestNormalizeMessageForStorageLeavesRegularMessagesUntouched(t *testing.T) {
	regular := &waProto.Message{
		Conversation: proto.String("plain text"),
	}
	evt := &events.Message{
		Info: types.MessageInfo{
			ID: types.MessageID("REGULAR-ID"),
		},
		Message: regular,
	}

	gotID, gotMessage, gotIsEdit := normalizeMessageForStorage(evt)
	if gotIsEdit {
		t.Fatalf("normalizeMessageForStorage() isEdit = true for a regular message")
	}
	if gotID != "REGULAR-ID" {
		t.Fatalf("normalizeMessageForStorage() id = %q, want %q", gotID, "REGULAR-ID")
	}
	if gotMessage != regular {
		t.Fatalf("normalizeMessageForStorage() changed a regular message payload unexpectedly")
	}
}

func TestStoreSentMessagePersistsOutboundText(t *testing.T) {
	t.Setenv("WHATSAPP_MCP_STORE_DIR", t.TempDir())

	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore() error = %v", err)
	}
	defer store.Close()

	chat := types.JID{User: "99900123456789", Server: "lid"}
	timestamp := time.Unix(1_700_000_000, 0)
	msg := &waProto.Message{Conversation: proto.String("outbound text")}

	if err := storeSentMessage(store, "15551230001@s.whatsapp.net", chat, "SENT-ID", timestamp, msg, ReplyMetadata{}); err != nil {
		t.Fatalf("storeSentMessage() error = %v", err)
	}

	var content, sender string
	var isFromMe bool
	if err := store.db.QueryRow(
		"SELECT content, sender, is_from_me FROM messages WHERE id = ? AND chat_jid = ?",
		"SENT-ID", chat.String(),
	).Scan(&content, &sender, &isFromMe); err != nil {
		t.Fatalf("SELECT sent message error = %v", err)
	}
	if content != "outbound text" || sender != "15551230001@s.whatsapp.net" || !isFromMe {
		t.Fatalf("sent message row = (%q, %q, %v), want (outbound text, 15551230001@s.whatsapp.net, true)", content, sender, isFromMe)
	}

	var chatCount int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM chats WHERE jid = ?", chat.String()).Scan(&chatCount); err != nil {
		t.Fatalf("SELECT chat error = %v", err)
	}
	if chatCount != 1 {
		t.Fatalf("chat rows = %d, want 1 (own sends must also upsert the chat)", chatCount)
	}
}

func waitForFile(t *testing.T, path string) string {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if data, err := os.ReadFile(path); err == nil {
			return string(data)
		}
		time.Sleep(20 * time.Millisecond)
	}
	return ""
}

func arrivalHookScript(t *testing.T) (script string, out string) {
	t.Helper()
	dir := t.TempDir()
	out = filepath.Join(dir, "fired.txt")
	script = filepath.Join(dir, "hook.sh")
	body := "#!/bin/sh\nprintf '%s|' \"$@\" > " + out + "\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatalf("write hook script: %v", err)
	}
	return script, out
}

func TestMediaArrivalHookFiresForAudio(t *testing.T) {
	script, out := arrivalHookScript(t)
	t.Setenv("WHATSAPP_MEDIA_ARRIVAL_HOOK", script)
	t.Setenv("WHATSAPP_MEDIA_ARRIVAL_HOOK_TYPES", "audio")

	runMediaArrivalHook("audio", "MSGID123", "5215550001111@s.whatsapp.net", waLog.Noop)

	got := waitForFile(t, out)
	want := "media|arrival-hook|audio|MSGID123|5215550001111@s.whatsapp.net|"
	if got != want {
		t.Fatalf("hook did not receive the arrival: got %q want %q", got, want)
	}
}

func TestMediaArrivalHookIgnoresOtherMediaTypes(t *testing.T) {
	script, out := arrivalHookScript(t)
	t.Setenv("WHATSAPP_MEDIA_ARRIVAL_HOOK", script)
	t.Setenv("WHATSAPP_MEDIA_ARRIVAL_HOOK_TYPES", "audio")

	runMediaArrivalHook("image", "MSGID123", "5215550001111@s.whatsapp.net", waLog.Noop)

	time.Sleep(300 * time.Millisecond)
	if _, err := os.Stat(out); err == nil {
		t.Fatal("hook fired for an image while restricted to audio")
	}
}

func TestMediaArrivalHookIsInertWhenUnset(t *testing.T) {
	_, out := arrivalHookScript(t)
	t.Setenv("WHATSAPP_MEDIA_ARRIVAL_HOOK", "")

	runMediaArrivalHook("audio", "MSGID123", "5215550001111@s.whatsapp.net", waLog.Noop)

	time.Sleep(300 * time.Millisecond)
	if _, err := os.Stat(out); err == nil {
		t.Fatal("hook fired with no hook configured")
	}
}

func TestMediaReconcileHookRunsBridgeBackfill(t *testing.T) {
	script, out := arrivalHookScript(t)
	t.Setenv("WHATSAPP_MEDIA_RECONCILE_HOOK", script)
	t.Setenv("WHATSAPP_MEDIA_RECONCILE_LIMIT", "17")
	t.Setenv("WHATSAPP_MEDIA_RECONCILE_LOOKBACK_DAYS", "9")
	mediaReconcileRunning.Store(false)
	mediaReconcileRequested.Store(false)

	runMediaReconcileHook(waLog.Noop)

	got := waitForFile(t, out)
	wantPrefix := "media|transcribe-pending|--since|" + time.Now().UTC().AddDate(0, 0, -9).Format("2006-01-02")
	if !strings.HasPrefix(got, wantPrefix) || !strings.HasSuffix(got, "|--limit|17|--drain|") {
		t.Fatalf("reconcile hook did not receive the bounded bridge backfill: got %q", got)
	}
}

func TestNormalizeMessageForStorageFindsEditNestedInEphemeralWrapper(t *testing.T) {
	editedPayload := &waProto.Message{
		Conversation: proto.String("Miércoles 26 a la 7:50 pm"),
	}

	evt := &events.Message{
		Info: types.MessageInfo{
			ID:        types.MessageID("EDIT-STANZA-ID"),
			Timestamp: time.Unix(1_700_000_000, 0),
		},
		Message: &waProto.Message{
			EphemeralMessage: &waProto.FutureProofMessage{
				Message: &waProto.Message{
					ProtocolMessage: &waProto.ProtocolMessage{
						Type: waProto.ProtocolMessage_MESSAGE_EDIT.Enum(),
						Key: &waProto.MessageKey{
							ID: proto.String("ORIGINAL-ID"),
						},
						EditedMessage: editedPayload,
					},
				},
			},
		},
	}

	gotID, gotMessage, gotIsEdit := normalizeMessageForStorage(evt)
	if !gotIsEdit {
		t.Fatalf("normalizeMessageForStorage() isEdit = false for a wrapped edit")
	}
	if gotID != "ORIGINAL-ID" {
		t.Fatalf("normalizeMessageForStorage() id = %q, want %q", gotID, "ORIGINAL-ID")
	}
	if got := extractTextContent(gotMessage); got != "Miércoles 26 a la 7:50 pm" {
		t.Fatalf("extractTextContent(normalized) = %q, want the edited text", got)
	}
}

func TestNormalizeMessageForStorageTrustsWhatsmeowEditFlagWithoutType(t *testing.T) {
	// An edit whose protocol message carries no explicit type; whatsmeow already
	// recognised it while unwrapping.
	evt := &events.Message{
		Info:   types.MessageInfo{ID: types.MessageID("EDIT-STANZA-ID")},
		IsEdit: true,
		Message: &waProto.Message{
			ProtocolMessage: &waProto.ProtocolMessage{
				Key:           &waProto.MessageKey{ID: proto.String("ORIGINAL-ID")},
				EditedMessage: &waProto.Message{Conversation: proto.String("revised")},
			},
		},
	}

	gotID, gotMessage, gotIsEdit := normalizeMessageForStorage(evt)
	if !gotIsEdit {
		t.Fatalf("normalizeMessageForStorage() isEdit = false, want true")
	}
	if gotID != "ORIGINAL-ID" {
		t.Fatalf("normalizeMessageForStorage() id = %q, want %q", gotID, "ORIGINAL-ID")
	}
	if got := extractTextContent(gotMessage); got != "revised" {
		t.Fatalf("extractTextContent(normalized) = %q, want %q", got, "revised")
	}
}

func TestApplyMessageEditRevisesTextAndKeepsReplyMetadata(t *testing.T) {
	t.Setenv("WHATSAPP_MCP_STORE_DIR", t.TempDir())

	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore() error = %v", err)
	}
	defer store.Close()

	original := time.Unix(1_700_000_000, 0)
	if err := store.StoreChat("chat@s.whatsapp.net", "Chat", original); err != nil {
		t.Fatalf("StoreChat() error = %v", err)
	}
	if err := store.StoreMessage(
		"ORIGINAL-ID", "chat@s.whatsapp.net", "15551230001@s.whatsapp.net",
		"Miércoles 26 a la 6:50 pm", original, false, "",
		ReplyMetadata{MessageID: "QUOTED-ID", Sender: "15551230002@s.whatsapp.net", Content: "quoted text"},
		"", "", nil, nil, nil, 0,
	); err != nil {
		t.Fatalf("StoreMessage() error = %v", err)
	}

	editedAt := original.Add(93 * time.Second)
	updated, err := store.ApplyMessageEdit(
		"ORIGINAL-ID", "chat@s.whatsapp.net", "Miércoles 26 a la 7:50 pm", editedAt,
		"", "", "", nil, nil, nil, 0,
	)
	if err != nil {
		t.Fatalf("ApplyMessageEdit() error = %v", err)
	}
	if !updated {
		t.Fatalf("ApplyMessageEdit() updated = false, want true")
	}

	var content, replyTo, replyContent string
	var storedTimestamp, editedStamp time.Time
	if err := store.db.QueryRow(
		"SELECT content, reply_to_message_id, reply_to_content, timestamp, edited_at FROM messages WHERE id = ? AND chat_jid = ?",
		"ORIGINAL-ID", "chat@s.whatsapp.net",
	).Scan(&content, &replyTo, &replyContent, &storedTimestamp, &editedStamp); err != nil {
		t.Fatalf("SELECT message error = %v", err)
	}

	if content != "Miércoles 26 a la 7:50 pm" {
		t.Fatalf("content = %q, want the edited text", content)
	}
	if replyTo != "QUOTED-ID" || replyContent != "quoted text" {
		t.Fatalf("edit wiped reply metadata: reply_to = %q, reply_content = %q", replyTo, replyContent)
	}
	if !storedTimestamp.Equal(original) {
		t.Fatalf("timestamp = %v, want the original send time %v", storedTimestamp, original)
	}
	if !editedStamp.Equal(editedAt) {
		t.Fatalf("edited_at = %v, want %v", editedStamp, editedAt)
	}
}

func TestApplyMessageEditReportsMissWhenOriginalUnknown(t *testing.T) {
	t.Setenv("WHATSAPP_MCP_STORE_DIR", t.TempDir())

	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore() error = %v", err)
	}
	defer store.Close()

	updated, err := store.ApplyMessageEdit(
		"MISSING-ID", "chat@s.whatsapp.net", "revised", time.Unix(1_700_000_000, 0),
		"", "", "", nil, nil, nil, 0,
	)
	if err != nil {
		t.Fatalf("ApplyMessageEdit() error = %v", err)
	}
	if updated {
		t.Fatalf("ApplyMessageEdit() updated = true for a message that was never stored")
	}
}

func TestDescribeMessagePayloadNamesFieldsWithoutContent(t *testing.T) {
	got := describeMessagePayload(&waProto.Message{
		Conversation: proto.String("secret text"),
	})
	if got != "conversation" {
		t.Fatalf("describeMessagePayload() = %q, want %q", got, "conversation")
	}
	if strings.Contains(got, "secret text") {
		t.Fatalf("describeMessagePayload() leaked message content: %q", got)
	}
	if got := describeMessagePayload(nil); got != "<nil>" {
		t.Fatalf("describeMessagePayload(nil) = %q, want <nil>", got)
	}
	if got := describeMessagePayload(&waProto.Message{}); got != "<empty>" {
		t.Fatalf("describeMessagePayload(empty) = %q, want <empty>", got)
	}
}

func TestEnsureMessageSchemaAddsEditedAtToExistingDatabase(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "messages.db")

	db, err := sql.Open("sqlite3", dbPath+"?_foreign_keys=on")
	if err != nil {
		t.Fatalf("sql.Open() error = %v", err)
	}
	defer db.Close()

	// A store predating edited_at, holding a message already on record.
	if _, err := db.Exec(`
		CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
		CREATE TABLE messages (
			id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
			is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT,
			media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
			PRIMARY KEY (id, chat_jid)
		);
		INSERT INTO messages (id, chat_jid, content) VALUES ('OLD-ID', 'chat@s.whatsapp.net', 'already stored');
	`); err != nil {
		t.Fatalf("seed legacy schema error = %v", err)
	}

	if err := ensureMessageSchema(db); err != nil {
		t.Fatalf("ensureMessageSchema() error = %v", err)
	}

	var content string
	var editedAt sql.NullTime
	if err := db.QueryRow("SELECT content, edited_at FROM messages WHERE id = 'OLD-ID'").Scan(&content, &editedAt); err != nil {
		t.Fatalf("SELECT after migration error = %v", err)
	}
	if content != "already stored" {
		t.Fatalf("migration disturbed stored content: %q", content)
	}
	if editedAt.Valid {
		t.Fatalf("edited_at = %v for a message that was never edited, want NULL", editedAt.Time)
	}
}
