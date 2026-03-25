#ifndef PYTTD_BINLOG_H
#define PYTTD_BINLOG_H

#include <stdint.h>
#include "frame_event.h"

/* Binlog event type encoding */
#define BINLOG_EVT_CALL             0
#define BINLOG_EVT_LINE             1
#define BINLOG_EVT_RETURN           2
#define BINLOG_EVT_EXCEPTION        3
#define BINLOG_EVT_EXCEPTION_UNWIND 4

/* File format constants */
#define BINLOG_MAGIC        "PYTTDBL\0"
#define BINLOG_MAGIC_SIZE   8
#define BINLOG_VERSION      1
#define BINLOG_HEADER_SIZE  64
#define BINLOG_RECORD_HEADER_SIZE 48

/* Core API */
int  binlog_open(const char *db_path, const char *run_id);
int  binlog_close(void);
void binlog_close_child(void);
int  binlog_write_batch(const FrameEvent *events, uint32_t count);
int  binlog_load_into_sqlite(const char *db_path);
uint64_t binlog_record_count(void);
uint64_t binlog_byte_count(void);

/* Size limit for auto-stop */
void binlog_set_size_limit(uint64_t max_bytes);

/* Python-facing functions */
#include <Python.h>
PyObject *pyttd_binlog_open(PyObject *self, PyObject *args);
PyObject *pyttd_binlog_load(PyObject *self, PyObject *args);
PyObject *pyttd_binlog_set_size_limit(PyObject *self, PyObject *args);

#endif /* PYTTD_BINLOG_H */
