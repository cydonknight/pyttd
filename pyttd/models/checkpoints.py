from peewee import AutoField, BigIntegerField, BooleanField, IntegerField, ForeignKeyField
from pyttd.models.base import _BaseModel
from pyttd.models.runs import Runs

class Checkpoint(_BaseModel):
    checkpoint_id = AutoField()
    run_id = ForeignKeyField(Runs, backref='checkpoints', field='run_id')
    sequence_no = BigIntegerField()
    child_pid = IntegerField(null=True)
    is_alive = BooleanField(default=True)

    class Meta:
        indexes = (
            (('run_id', 'sequence_no'), False),
        )
