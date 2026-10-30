"""Complete, transactional migration history ships in each full release."""
CURRENT=1

def first(db):
    # Additive bookkeeping only; no business row is rewritten.
    db.execute('CREATE TABLE IF NOT EXISTS installation_schema(version INTEGER NOT NULL)')

MIGRATIONS={1:first}

def migrate(db,target=CURRENT,migrations=None):
    steps=MIGRATIONS if migrations is None else migrations
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='installation_schema'").fetchone()
    row=db.execute('SELECT version FROM installation_schema').fetchone() if exists else None
    current=row[0] if row else 0
    if current>target:raise ValueError('Database schema newer than runtime; refuse old writer')
    if any(i not in steps for i in range(current+1,target+1)):raise ValueError('Missing migration path')
    db.execute('SAVEPOINT installation_migrate')
    try:
        for version in range(current+1,target+1):
            steps[version](db)
            db.execute('CREATE TABLE IF NOT EXISTS installation_schema(version INTEGER NOT NULL)')
            db.execute('DELETE FROM installation_schema');db.execute('INSERT INTO installation_schema VALUES (?)',(version,))
        db.execute('RELEASE installation_migrate')
    except BaseException:
        db.execute('ROLLBACK TO installation_migrate');db.execute('RELEASE installation_migrate')
        raise
    return target
