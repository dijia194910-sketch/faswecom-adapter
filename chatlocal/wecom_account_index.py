"""Transactional account ownership counts for imported WeCom messages.

The account is the prefix of the canonical conversation ID. Counts include
malformed IDs under the empty account, so an export still fails closed.
"""

TRIGGERS = ('wecom_account_insert', 'wecom_account_delete', 'wecom_account_move')


def ready(db):
    names = ('wecom_account_index', *TRIGGERS)
    return db.execute(
        "SELECT count(*) FROM sqlite_master WHERE name IN (?,?,?,?)", names
    ).fetchone()[0] == len(names)


def initialize(db):
    if ready(db):
        return
    # Rebuild and install maintenance together. Interrupted migrations cannot
    # leave an apparently usable partial account index.
    db.executescript('''
      BEGIN IMMEDIATE;
      CREATE TABLE IF NOT EXISTS wecom_account_index(
        account TEXT PRIMARY KEY, count INTEGER NOT NULL CHECK(count>=0));
      DELETE FROM wecom_account_index;
      INSERT INTO wecom_account_index
        SELECT substr(conversation_id,1,instr(conversation_id,':')-1),count(*)
        FROM messages WHERE platform='wecom' GROUP BY 1;
      DROP TRIGGER IF EXISTS wecom_account_insert;
      DROP TRIGGER IF EXISTS wecom_account_delete;
      DROP TRIGGER IF EXISTS wecom_account_move;
      CREATE TRIGGER wecom_account_insert AFTER INSERT ON messages WHEN NEW.platform='wecom' BEGIN
        INSERT INTO wecom_account_index VALUES(substr(NEW.conversation_id,1,instr(NEW.conversation_id,':')-1),1)
          ON CONFLICT(account) DO UPDATE SET count=count+1;
      END;
      CREATE TRIGGER wecom_account_delete AFTER DELETE ON messages WHEN OLD.platform='wecom' BEGIN
        UPDATE wecom_account_index SET count=count-1
          WHERE account=substr(OLD.conversation_id,1,instr(OLD.conversation_id,':')-1);
        DELETE FROM wecom_account_index WHERE count=0;
      END;
      CREATE TRIGGER wecom_account_move AFTER UPDATE OF platform,conversation_id ON messages
      WHEN OLD.platform<>NEW.platform OR OLD.conversation_id<>NEW.conversation_id BEGIN
        UPDATE wecom_account_index SET count=count-1
          WHERE OLD.platform='wecom' AND account=substr(OLD.conversation_id,1,instr(OLD.conversation_id,':')-1);
        DELETE FROM wecom_account_index WHERE count=0;
        INSERT INTO wecom_account_index
          SELECT substr(NEW.conversation_id,1,instr(NEW.conversation_id,':')-1),1 WHERE NEW.platform='wecom'
          ON CONFLICT(account) DO UPDATE SET count=count+1;
      END;
      COMMIT;
    ''')


def validate(db, account):
    if ready(db):
        foreign = db.execute('SELECT 1 FROM wecom_account_index WHERE account<>? AND count>0 LIMIT 1', (account,)).fetchone()
    else:
        # Read-only callers can open an older archive before Store migrates it.
        # Keep the original check rather than silently trusting absent metadata.
        foreign = db.execute("SELECT 1 FROM messages WHERE platform='wecom' AND substr(conversation_id,1,?)<>? LIMIT 1",
                             (len(account)+1, account+':')).fetchone()
    if foreign:
        raise ValueError('消息库中存在与当前账号不一致的记录，请先核对账号来源')
