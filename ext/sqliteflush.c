#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <sqlite3.h>

#ifndef _WIN32
#include <sys/stat.h>
#include <limits.h>
#else
#include <windows.h>
#ifndef PATH_MAX
#define PATH_MAX MAX_PATH
#endif
#endif

#include "sqliteflush.h"
#include "recorder.h"  /* g_stop_requested */

/* ---- State ---- */

static sqlite3 *g_flush_db = NULL;
static sqlite3_stmt *g_insert_stmt = NULL;
static char g_run_id_str[128] = {0};
static char g_db_path[1024] = {0};
static uint64_t g_size_limit = 0;       /* 0 = unlimited */
static uint64_t g_flush_batch_count = 0; /* for throttled size checks */

/* ---- Realpath Cache ---- */

#define REALPATH_CACHE_SIZE 128
#define REALPATH_CACHE_MASK (REALPATH_CACHE_SIZE - 1)

typedef struct {
    const char *original;    /* source pointer (interned from CPython code objects) */
    char resolved[PATH_MAX]; /* resolved path */
    int valid;
} RealpathEntry;

static RealpathEntry g_realpath_cache[REALPATH_CACHE_SIZE];

static unsigned realpath_hash(const char *ptr) {
    uintptr_t v = (uintptr_t)ptr;
    v ^= v >> 16;
    v *= 0x45d9f3b;
    v ^= v >> 16;
    return (unsigned)(v & REALPATH_CACHE_MASK);
}

static const char *resolve_filename(const char *filename) {
    if (!filename) return "";

    unsigned idx = realpath_hash(filename);
    RealpathEntry *entry = &g_realpath_cache[idx];

    /* Pointer equality check (interned strings from code objects) */
    if (entry->valid && entry->original == filename) {
        return entry->resolved;
    }

    /* Pointer miss — check by string content for hash collisions */
    if (entry->valid && strcmp(entry->original, filename) == 0) {
        /* Same string, different pointer — update pointer for faster future hits */
        entry->original = filename;
        return entry->resolved;
    }

    /* Cache miss — resolve */
#ifndef _WIN32
    char *resolved = realpath(filename, entry->resolved);
    if (!resolved) {
        /* realpath failed (file doesn't exist, etc.) — use original */
        strncpy(entry->resolved, filename, PATH_MAX - 1);
        entry->resolved[PATH_MAX - 1] = '\0';
    }
#else
    DWORD len = GetFullPathNameA(filename, PATH_MAX, entry->resolved, NULL);
    if (len == 0 || len >= PATH_MAX) {
        strncpy(entry->resolved, filename, PATH_MAX - 1);
        entry->resolved[PATH_MAX - 1] = '\0';
    }
#endif

    entry->original = filename;
    entry->valid = 1;
    return entry->resolved;
}

/* ---- SQLite Operations ---- */

static const char *INSERT_SQL =
    "INSERT INTO executionframes "
    "(run_id, sequence_no, timestamp, line_no, filename, function_name, "
    "frame_event, call_depth, locals_snapshot, thread_id, is_coroutine) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)";

int sqliteflush_open(const char *db_path) {
    if (g_flush_db) {
        sqliteflush_close();
    }

    strncpy(g_db_path, db_path, sizeof(g_db_path) - 1);
    g_db_path[sizeof(g_db_path) - 1] = '\0';

    int rc = sqlite3_open(db_path, &g_flush_db);
    if (rc != SQLITE_OK) {
        g_flush_db = NULL;
        return -1;
    }

    /* Set pragmas matching pyttd/models/constants.py PRAGMAS */
    sqlite3_exec(g_flush_db, "PRAGMA journal_mode = wal", NULL, NULL, NULL);
    sqlite3_exec(g_flush_db, "PRAGMA cache_size = -65536", NULL, NULL, NULL);
    sqlite3_exec(g_flush_db, "PRAGMA foreign_keys = 1", NULL, NULL, NULL);
    sqlite3_exec(g_flush_db, "PRAGMA synchronous = 1", NULL, NULL, NULL);
    sqlite3_exec(g_flush_db, "PRAGMA busy_timeout = 5000", NULL, NULL, NULL);

    /* Prepare INSERT statement */
    rc = sqlite3_prepare_v2(g_flush_db, INSERT_SQL, -1, &g_insert_stmt, NULL);
    if (rc != SQLITE_OK) {
        sqlite3_close(g_flush_db);
        g_flush_db = NULL;
        g_insert_stmt = NULL;
        return -1;
    }

    /* Reset state */
    g_flush_batch_count = 0;
    memset(g_realpath_cache, 0, sizeof(g_realpath_cache));

    return 0;
}

int sqliteflush_close(void) {
    if (g_insert_stmt) {
        sqlite3_finalize(g_insert_stmt);
        g_insert_stmt = NULL;
    }
    if (g_flush_db) {
        sqlite3_close(g_flush_db);
        g_flush_db = NULL;
    }
    g_run_id_str[0] = '\0';
    g_db_path[0] = '\0';
    g_size_limit = 0;
    g_flush_batch_count = 0;
    memset(g_realpath_cache, 0, sizeof(g_realpath_cache));
    return 0;
}

void sqliteflush_close_child(void) {
    /* In checkpoint child: do NOT call sqlite3_close or any SQLite API.
     * In WAL mode, sqlite3_close modifies shared WAL/SHM files.
     * Just NULL out the pointers and let _exit() close FDs at OS level. */
    g_flush_db = NULL;
    g_insert_stmt = NULL;
}

void sqliteflush_set_run_id(const char *run_id) {
    strncpy(g_run_id_str, run_id, sizeof(g_run_id_str) - 1);
    g_run_id_str[sizeof(g_run_id_str) - 1] = '\0';
}

void sqliteflush_set_size_limit(uint64_t max_bytes) {
    g_size_limit = max_bytes;
}

void sqliteflush_check_size_limit(void) {
    if (g_size_limit == 0 || g_db_path[0] == '\0') return;

#ifndef _WIN32
    struct stat st;
    if (stat(g_db_path, &st) == 0) {
        if ((uint64_t)st.st_size >= g_size_limit) {
            atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
        }
    }
#else
    WIN32_FILE_ATTRIBUTE_DATA fad;
    if (GetFileAttributesExA(g_db_path, GetFileExInfoStandard, &fad)) {
        uint64_t size = ((uint64_t)fad.nFileSizeHigh << 32) | fad.nFileSizeLow;
        if (size >= g_size_limit) {
            atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
        }
    }
#endif
}

int sqliteflush_insert_batch(const FrameEvent *events, uint32_t count) {
    if (!g_flush_db || !g_insert_stmt || count == 0) return -1;

    int rc = sqlite3_exec(g_flush_db, "BEGIN", NULL, NULL, NULL);
    if (rc != SQLITE_OK) return -1;

    for (uint32_t i = 0; i < count; i++) {
        const FrameEvent *e = &events[i];
        const char *resolved_filename = resolve_filename(e->filename);

        sqlite3_bind_text(g_insert_stmt, 1, g_run_id_str, -1, SQLITE_STATIC);
        sqlite3_bind_int64(g_insert_stmt, 2, (sqlite3_int64)e->sequence_no);
        sqlite3_bind_double(g_insert_stmt, 3, e->timestamp);
        sqlite3_bind_int(g_insert_stmt, 4, e->line_no);
        sqlite3_bind_text(g_insert_stmt, 5, resolved_filename, -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(g_insert_stmt, 6, e->function_name ? e->function_name : "", -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(g_insert_stmt, 7, e->event_type ? e->event_type : "line", -1, SQLITE_STATIC);
        sqlite3_bind_int(g_insert_stmt, 8, e->call_depth);
        if (e->locals_json) {
            sqlite3_bind_text(g_insert_stmt, 9, e->locals_json, -1, SQLITE_TRANSIENT);
        } else {
            sqlite3_bind_null(g_insert_stmt, 9);
        }
        sqlite3_bind_int64(g_insert_stmt, 10, (sqlite3_int64)e->thread_id);
        sqlite3_bind_int(g_insert_stmt, 11, e->is_coroutine);

        rc = sqlite3_step(g_insert_stmt);
        sqlite3_reset(g_insert_stmt);
        if (rc != SQLITE_DONE) {
            continue;
        }
    }

    sqlite3_exec(g_flush_db, "COMMIT", NULL, NULL, NULL);

    /* Throttled size check — every 100 batches */
    g_flush_batch_count++;
    if (g_size_limit > 0 && (g_flush_batch_count % 100) == 0) {
        sqliteflush_check_size_limit();
    }

    return 0;
}

/* ---- Python-facing functions ---- */

PyObject *pyttd_set_flush_db(PyObject *self, PyObject *args) {
    (void)self;
    const char *db_path;
    const char *run_id_str;
    if (!PyArg_ParseTuple(args, "ss", &db_path, &run_id_str)) return NULL;

    if (sqliteflush_open(db_path) != 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "Failed to open SQLite connection for flush thread");
        return NULL;
    }
    sqliteflush_set_run_id(run_id_str);
    Py_RETURN_NONE;
}

PyObject *pyttd_set_flush_size_limit(PyObject *self, PyObject *args) {
    (void)self;
    unsigned long long limit;
    if (!PyArg_ParseTuple(args, "K", &limit)) return NULL;
    sqliteflush_set_size_limit((uint64_t)limit);
    Py_RETURN_NONE;
}
