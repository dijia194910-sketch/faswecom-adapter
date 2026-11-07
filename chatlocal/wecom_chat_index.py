"""Incremental conversation metadata: opening a page never scans message bodies."""


def initialize(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='wecom_chat_index'").fetchone():return
    db.executescript('''
      BEGIN IMMEDIATE;
      CREATE TABLE IF NOT EXISTS wecom_chat_index(
        conversation_id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,
        count INTEGER NOT NULL,latest INTEGER NOT NULL,latest_id INTEGER NOT NULL);
      INSERT OR IGNORE INTO wecom_chat_index
        SELECT conversation_id,max(conversation),max(conversation_type),count(*),max(timestamp),
          (SELECT id FROM messages b WHERE b.platform='wecom' AND b.conversation_id=a.conversation_id
            ORDER BY timestamp DESC,id DESC LIMIT 1)
        FROM messages a WHERE platform='wecom' GROUP BY conversation_id;
      CREATE INDEX IF NOT EXISTS wecom_chat_latest ON wecom_chat_index(latest DESC,latest_id DESC);
      CREATE TRIGGER IF NOT EXISTS wecom_chat_insert AFTER INSERT ON messages WHEN NEW.platform='wecom' BEGIN
        INSERT INTO wecom_chat_index VALUES(NEW.conversation_id,NEW.conversation,NEW.conversation_type,1,NEW.timestamp,NEW.id)
        ON CONFLICT(conversation_id) DO UPDATE SET count=count+1,
          name=CASE WHEN (NEW.timestamp,NEW.id)>=(latest,latest_id) THEN NEW.conversation ELSE name END,
          kind=CASE WHEN (NEW.timestamp,NEW.id)>=(latest,latest_id) THEN NEW.conversation_type ELSE kind END,
          latest_id=CASE WHEN (NEW.timestamp,NEW.id)>=(latest,latest_id) THEN NEW.id ELSE latest_id END,
          latest=max(latest,NEW.timestamp);
      END;
      CREATE TRIGGER IF NOT EXISTS wecom_chat_delete AFTER DELETE ON messages WHEN OLD.platform='wecom' BEGIN
        UPDATE wecom_chat_index SET count=count-1 WHERE conversation_id=OLD.conversation_id;
        DELETE FROM wecom_chat_index WHERE conversation_id=OLD.conversation_id AND count=0;
        UPDATE wecom_chat_index SET (latest,latest_id,name,kind)=(
          SELECT timestamp,id,conversation,conversation_type FROM messages
          WHERE platform='wecom' AND conversation_id=OLD.conversation_id ORDER BY timestamp DESC,id DESC LIMIT 1)
          WHERE conversation_id=OLD.conversation_id AND latest_id=OLD.id;
      END;
      CREATE TRIGGER IF NOT EXISTS wecom_chat_edit AFTER UPDATE OF content,conversation,conversation_type,timestamp ON messages
      WHEN NEW.platform='wecom' BEGIN
        UPDATE wecom_chat_index SET name=NEW.conversation,kind=NEW.conversation_type WHERE conversation_id=NEW.conversation_id;
        UPDATE wecom_chat_index SET (latest,latest_id)=(SELECT timestamp,id FROM messages
          WHERE platform='wecom' AND conversation_id=NEW.conversation_id ORDER BY timestamp DESC,id DESC LIMIT 1)
          WHERE conversation_id=NEW.conversation_id AND (latest_id=NEW.id OR NEW.timestamp>=latest);
      END;
      CREATE TRIGGER IF NOT EXISTS wecom_chat_move_out AFTER UPDATE OF platform,conversation_id ON messages
      WHEN OLD.platform='wecom' AND (NEW.platform<>OLD.platform OR NEW.conversation_id<>OLD.conversation_id) BEGIN
        UPDATE wecom_chat_index SET count=count-1 WHERE conversation_id=OLD.conversation_id;
        DELETE FROM wecom_chat_index WHERE conversation_id=OLD.conversation_id AND count=0;
        UPDATE wecom_chat_index SET (latest,latest_id,name,kind)=(SELECT timestamp,id,conversation,conversation_type FROM messages
          WHERE platform='wecom' AND conversation_id=OLD.conversation_id ORDER BY timestamp DESC,id DESC LIMIT 1)
          WHERE conversation_id=OLD.conversation_id AND latest_id=OLD.id;
      END;
      CREATE TRIGGER IF NOT EXISTS wecom_chat_move_in AFTER UPDATE OF platform,conversation_id ON messages
      WHEN NEW.platform='wecom' AND (NEW.platform<>OLD.platform OR NEW.conversation_id<>OLD.conversation_id) BEGIN
        INSERT INTO wecom_chat_index VALUES(NEW.conversation_id,NEW.conversation,NEW.conversation_type,1,NEW.timestamp,NEW.id)
        ON CONFLICT(conversation_id) DO UPDATE SET count=count+1,
          latest_id=CASE WHEN (NEW.timestamp,NEW.id)>=(latest,latest_id) THEN NEW.id ELSE latest_id END,
          latest=max(latest,NEW.timestamp);
      END;
      COMMIT;
    ''')
