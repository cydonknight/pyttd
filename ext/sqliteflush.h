#ifndef PYTTD_SQLITEFLUSH_H
#define PYTTD_SQLITEFLUSH_H

#include <stdint.h>
#include "frame_event.h"

/* Open a dedicated sqlite3 connection for the flush thread.
 * Sets pragmas matching constants.py (WAL, cache_size, busy_timeout, etc.).
 * Prepares the INSERT statement as a compiled sqlite3_stmt. */
int sqliteflush_open(const char *db_path);

/* Finalize prepared statement, close connection. Commits pending transaction. */
int sqliteflush_close(void);

/* Close inherited connection in checkpoint child (no COMMIT — parent owns data). */
void sqliteflush_close_child(void);

/* Store run_id string (called once from Python after Runs.create()). */
void sqliteflush_set_run_id(const char *run_id);

/* INSERT a batch of FrameEvents via prepared statement.
 * Executes BEGIN, binds + steps each event, then COMMIT.
 * Resolves filenames via realpath() with internal cache.
 * Does NOT require the Python GIL. */
int sqliteflush_insert_batch(const FrameEvent *events, uint32_t count);

/* Set DB file size limit in bytes. 0 = unlimited.
 * Checked every 100 flush batches via stat(). */
void sqliteflush_set_size_limit(uint64_t max_bytes);

/* Check DB file size. If >= limit, sets g_stop_requested.
 * Called internally by sqliteflush_insert_batch every N batches. */
void sqliteflush_check_size_limit(void);

/* Python-facing function: set_flush_db(db_path, run_id_str) */
#include <Python.h>
PyObject *pyttd_set_flush_db(PyObject *self, PyObject *args);
PyObject *pyttd_set_flush_size_limit(PyObject *self, PyObject *args);

#endif /* PYTTD_SQLITEFLUSH_H */
