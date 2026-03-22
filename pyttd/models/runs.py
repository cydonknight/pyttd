from uuid import uuid4
from datetime import datetime
from peewee import UUIDField, FloatField, CharField, IntegerField, BooleanField
from pyttd.models.base import _BaseModel

class Runs(_BaseModel):
    run_id = UUIDField(unique=True, primary_key=True, default=uuid4)
    timestamp_start = FloatField(default=lambda: datetime.now().timestamp())
    timestamp_end = FloatField(null=True)
    script_path = CharField(null=True)
    total_frames = IntegerField(default=0)
    is_attach = BooleanField(default=False)
