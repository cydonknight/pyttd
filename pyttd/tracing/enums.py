from enum import StrEnum

class EventENUM(StrEnum):
    CALL = 'call'
    LINE = 'line'
    RETURN = 'return'
    EXCEPTION = 'exception'
    EXCEPTION_UNWIND = 'exception_unwind'
