from peewee import AutoField, CharField, BigIntegerField, BlobField, ForeignKeyField
from pyttd.models.base import _BaseModel
from pyttd.models.runs import Runs


class IOEvent(_BaseModel):
    io_event_id = AutoField()
    run_id = ForeignKeyField(Runs, backref='io_events', field='run_id')
    sequence_no = BigIntegerField()
    io_sequence = BigIntegerField()
    function_name = CharField()
    return_value = BlobField()

    class Meta:
        indexes = (
            (('run_id', 'sequence_no', 'io_sequence'), True),
        )
