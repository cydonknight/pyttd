from peewee import (AutoField, CharField, IntegerField, BigIntegerField, TextField,
                    ForeignKeyField, FloatField, BooleanField)
from pyttd.models.base import _BaseModel
from pyttd.models.runs import Runs

class ExecutionFrames(_BaseModel):
    frame_id = AutoField()
    run_id = ForeignKeyField(Runs, backref='frames', field='run_id')
    sequence_no = BigIntegerField()
    timestamp = FloatField()
    line_no = IntegerField()
    filename = CharField()
    function_name = CharField()
    frame_event = CharField()
    call_depth = IntegerField()
    locals_snapshot = TextField(null=True)
    thread_id = BigIntegerField(default=0)
    is_coroutine = BooleanField(default=False)

    class Meta:
        indexes = (
            (('run_id', 'sequence_no'), True),
            (('run_id', 'filename', 'line_no'), False),
            (('run_id', 'function_name'), False),
            (('run_id', 'frame_event', 'sequence_no'), False),
            (('run_id', 'call_depth', 'sequence_no'), False),
            (('run_id', 'thread_id', 'sequence_no'), False),
        )
