from peewee import Model, SqliteDatabase

# Deferred database — initialized later via db.init(path)
db = SqliteDatabase(None)

class _BaseModel(Model):
    class Meta:
        database = db
