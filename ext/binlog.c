#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <stdatomic.h>
#include <sqlite3.h>

#ifndef _WIN32
#include <sys/stat.h>
#include <limits.h>
#include <unistd.h>
#else
#include <windows.h>
#ifndef PATH_MAX
#define PATH_MAX MAX_PATH
#endif
#endif

#include "binlog.h"
#include "recorder.h"  /* g_stop_requested */

/* ---- State ---- */

static FILE *g_binlog_fp = NULL;
static char g_binlog_path[1024] = {0};
static uint64_t g_binlog_records = 0;
static uint64_t g_binlog_bytes = 0;
static char g_binlog_run_id[64] = {0};
static uint64_t g_size_limit = 0;
static uint64_t g_batch_count = 0;

/* 256KB write buffer for stdio */
static char g_write_buf[256 * 1024];

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

    /* Pointer miss -- check by string content for hash collisions */
    if (entry->valid && strcmp(entry->original, filename) == 0) {
        entry->original = filename;
        return entry->resolved;
    }

    /* Cache miss -- resolve */
#ifndef _WIN32
    char *resolved = realpath(filename, entry->resolved);
    if (!resolved) {
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

/* ---- Event Type Mapping ---- */

static uint8_t map_event_type(const char *event_type) {
    if (!event_type) return BINLOG_EVT_LINE;
    /* Fast first-char dispatch against known event type strings */
    if (event_type[0] == 'c') return BINLOG_EVT_CALL;
    if (event_type[0] == 'l') return BINLOG_EVT_LINE;
    if (event_type[0] == 'r') return BINLOG_EVT_RETURN;
    if (event_type[0] == 'e') {
        if (strlen(event_type) > 9 && event_type[9] == '_')
            return BINLOG_EVT_EXCEPTION_UNWIND;
        return BINLOG_EVT_EXCEPTION;
    }
    return BINLOG_EVT_LINE;
}

static const char *unmap_event_type(uint8_t evt) {
    switch (evt) {
        case BINLOG_EVT_CALL:             return "call";
        case BINLOG_EVT_LINE:             return "line";
        case BINLOG_EVT_RETURN:           return "return";
        case BINLOG_EVT_EXCEPTION:        return "exception";
        case BINLOG_EVT_EXCEPTION_UNWIND: return "exception_unwind";
        default:                          return "line";
    }
}

/* ---- Size Limit ---- */

void binlog_set_size_limit(uint64_t max_bytes) {
    g_size_limit = max_bytes;
}

static void check_size_limit(void) {
    if (g_size_limit == 0 || g_binlog_path[0] == '\0') return;

#ifndef _WIN32
    struct stat st;
    if (stat(g_binlog_path, &st) == 0) {
        if ((uint64_t)st.st_size >= g_size_limit) {
            atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
        }
    }
#else
    WIN32_FILE_ATTRIBUTE_DATA fad;
    if (GetFileAttributesExA(g_binlog_path, GetFileExInfoStandard, &fad)) {
        uint64_t size = ((uint64_t)fad.nFileSizeHigh << 32) | fad.nFileSizeLow;
        if (size >= g_size_limit) {
            atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
        }
    }
#endif
}

/* ---- Binlog Path Computation ---- */

static void compute_binlog_path(const char *db_path, char *out, size_t out_size) {
    const char *suffix = ".pyttd.db";
    size_t db_len = strlen(db_path);
    size_t suffix_len = strlen(suffix);

    if (db_len > suffix_len &&
        strcmp(db_path + db_len - suffix_len, suffix) == 0) {
        size_t base_len = db_len - suffix_len;
        snprintf(out, out_size, "%.*s.pyttd.binlog", (int)base_len, db_path);
    } else {
        snprintf(out, out_size, "%s.binlog", db_path);
    }
}

/* ---- Core API ---- */

int binlog_open(const char *db_path, const char *run_id) {
    if (g_binlog_fp) {
        binlog_close();
    }

    compute_binlog_path(db_path, g_binlog_path, sizeof(g_binlog_path));

    g_binlog_fp = fopen(g_binlog_path, "wb");
    if (!g_binlog_fp) {
        g_binlog_path[0] = '\0';
        return -1;
    }

    /* Set 256KB fully-buffered I/O */
    setvbuf(g_binlog_fp, g_write_buf, _IOFBF, sizeof(g_write_buf));

    /* Write 64-byte file header */
    uint8_t header[BINLOG_HEADER_SIZE];
    memset(header, 0, sizeof(header));

    memcpy(header + 0, BINLOG_MAGIC, BINLOG_MAGIC_SIZE);

    uint16_t version = BINLOG_VERSION;
    memcpy(header + 8, &version, 2);

    uint16_t hdr_size = BINLOG_HEADER_SIZE;
    memcpy(header + 10, &hdr_size, 2);

    /* run_id at offset 16, 32 bytes NUL-padded */
    size_t rid_len = strlen(run_id);
    if (rid_len > 32) rid_len = 32;
    memcpy(header + 16, run_id, rid_len);

    if (fwrite(header, BINLOG_HEADER_SIZE, 1, g_binlog_fp) != 1) {
        fclose(g_binlog_fp);
        g_binlog_fp = NULL;
        g_binlog_path[0] = '\0';
        return -1;
    }

    strncpy(g_binlog_run_id, run_id, sizeof(g_binlog_run_id) - 1);
    g_binlog_run_id[sizeof(g_binlog_run_id) - 1] = '\0';
    g_binlog_records = 0;
    g_binlog_bytes = BINLOG_HEADER_SIZE;
    g_batch_count = 0;
    g_size_limit = 0;
    memset(g_realpath_cache, 0, sizeof(g_realpath_cache));

    return 0;
}

int binlog_close(void) {
    if (g_binlog_fp) {
        fflush(g_binlog_fp);
        fclose(g_binlog_fp);
        g_binlog_fp = NULL;
    }
    g_binlog_path[0] = '\0';
    g_binlog_run_id[0] = '\0';
    g_binlog_records = 0;
    g_binlog_bytes = 0;
    g_size_limit = 0;
    g_batch_count = 0;
    memset(g_realpath_cache, 0, sizeof(g_realpath_cache));
    return 0;
}

void binlog_close_child(void) {
    if (g_binlog_fp) {
        fclose(g_binlog_fp);
        g_binlog_fp = NULL;
    }
}

int binlog_write_batch(const FrameEvent *events, uint32_t count) {
    if (!g_binlog_fp || count == 0) return -1;

    for (uint32_t i = 0; i < count; i++) {
        const FrameEvent *e = &events[i];

        const char *resolved = resolve_filename(e->filename);
        const char *funcname = e->function_name ? e->function_name : "";
        const char *locals = e->locals_json;

        uint16_t filename_len = (uint16_t)strlen(resolved);
        uint16_t funcname_len = (uint16_t)strlen(funcname);
        uint32_t locals_len = locals ? (uint32_t)strlen(locals) : 0;

        uint32_t total_size = BINLOG_RECORD_HEADER_SIZE +
                              filename_len + funcname_len + locals_len;

        /* Build 48-byte record header on stack */
        uint8_t hdr[BINLOG_RECORD_HEADER_SIZE];
        memset(hdr, 0, sizeof(hdr));

        memcpy(hdr + 0, &total_size, 4);

        uint64_t seq = e->sequence_no;
        memcpy(hdr + 4, &seq, 8);

        double ts = e->timestamp;
        memcpy(hdr + 12, &ts, 8);

        int32_t line = (int32_t)e->line_no;
        memcpy(hdr + 20, &line, 4);

        int32_t depth = (int32_t)e->call_depth;
        memcpy(hdr + 24, &depth, 4);

        uint64_t tid = (uint64_t)e->thread_id;
        memcpy(hdr + 28, &tid, 8);

        hdr[36] = map_event_type(e->event_type);
        hdr[37] = (uint8_t)e->is_coroutine;

        memcpy(hdr + 38, &filename_len, 2);
        memcpy(hdr + 40, &funcname_len, 2);
        memcpy(hdr + 42, &locals_len, 4);
        /* hdr[46..47] reserved, already zero */

        if (fwrite(hdr, BINLOG_RECORD_HEADER_SIZE, 1, g_binlog_fp) != 1) {
            continue;
        }
        int write_ok = 1;
        if (filename_len > 0 && fwrite(resolved, filename_len, 1, g_binlog_fp) != 1)
            write_ok = 0;
        if (write_ok && funcname_len > 0 && fwrite(funcname, funcname_len, 1, g_binlog_fp) != 1)
            write_ok = 0;
        if (write_ok && locals_len > 0 && fwrite(locals, locals_len, 1, g_binlog_fp) != 1)
            write_ok = 0;
        if (!write_ok) break;  /* partial record written — file is corrupt, stop */

        g_binlog_records++;
        g_binlog_bytes += total_size;
    }

    g_batch_count++;
    if (g_size_limit > 0 && (g_batch_count % 100) == 0) {
        check_size_limit();
    }

    return 0;
}

uint64_t binlog_record_count(void) {
    return g_binlog_records;
}

uint64_t binlog_byte_count(void) {
    return g_binlog_bytes;
}

/* ---- Bulk Loader (binlog -> SQLite) ---- */

static const char *BULK_INSERT_SQL =
    "INSERT INTO executionframes "
    "(run_id, sequence_no, timestamp, line_no, filename, function_name, "
    "frame_event, call_depth, locals_snapshot, thread_id, is_coroutine) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)";

int binlog_load_into_sqlite(const char *db_path) {
    char binlog_path[1024];
    compute_binlog_path(db_path, binlog_path, sizeof(binlog_path));

    FILE *fp = fopen(binlog_path, "rb");
    if (!fp) {
        /* No binlog file -- nothing to load */
        return 0;
    }

    /* Read and validate file header */
    uint8_t file_header[BINLOG_HEADER_SIZE];
    if (fread(file_header, BINLOG_HEADER_SIZE, 1, fp) != 1) {
        fclose(fp);
        return -1;
    }

    if (memcmp(file_header, BINLOG_MAGIC, BINLOG_MAGIC_SIZE) != 0) {
        fclose(fp);
        return -1;
    }

    uint16_t version;
    memcpy(&version, file_header + 8, 2);
    if (version != BINLOG_VERSION) {
        fclose(fp);
        return -1;
    }

    char run_id[33];
    memcpy(run_id, file_header + 16, 32);
    run_id[32] = '\0';

    /* Open SQLite connection */
    sqlite3 *sqldb = NULL;
    int rc = sqlite3_open(db_path, &sqldb);
    if (rc != SQLITE_OK) {
        fclose(fp);
        return -1;
    }

    sqlite3_exec(sqldb, "PRAGMA journal_mode = wal", NULL, NULL, NULL);
    sqlite3_exec(sqldb, "PRAGMA cache_size = -65536", NULL, NULL, NULL);
    sqlite3_exec(sqldb, "PRAGMA synchronous = 1", NULL, NULL, NULL);
    sqlite3_exec(sqldb, "PRAGMA busy_timeout = 5000", NULL, NULL, NULL);

    sqlite3_stmt *stmt = NULL;
    rc = sqlite3_prepare_v2(sqldb, BULK_INSERT_SQL, -1, &stmt, NULL);
    if (rc != SQLITE_OK) {
        sqlite3_close(sqldb);
        fclose(fp);
        return -1;
    }

    sqlite3_exec(sqldb, "BEGIN", NULL, NULL, NULL);

    /* Read buffer for string payloads */
    char stack_buf[8192];
    char *heap_buf = NULL;
    size_t heap_buf_size = 0;

    uint64_t record_count = 0;
    int error = 0;

    while (!feof(fp)) {
        uint32_t total_size;
        if (fread(&total_size, 4, 1, fp) != 1) {
            break;
        }

        if (total_size < BINLOG_RECORD_HEADER_SIZE) {
            error = 1;
            break;
        }

        uint8_t hdr[BINLOG_RECORD_HEADER_SIZE];
        memcpy(hdr, &total_size, 4);
        if (fread(hdr + 4, BINLOG_RECORD_HEADER_SIZE - 4, 1, fp) != 1) {
            break;
        }

        uint64_t seq;
        memcpy(&seq, hdr + 4, 8);

        double timestamp;
        memcpy(&timestamp, hdr + 12, 8);

        int32_t line_no;
        memcpy(&line_no, hdr + 20, 4);

        int32_t call_depth;
        memcpy(&call_depth, hdr + 24, 4);

        uint64_t thread_id;
        memcpy(&thread_id, hdr + 28, 8);

        uint8_t event_type = hdr[36];
        uint8_t is_coroutine = hdr[37];

        uint16_t filename_len;
        memcpy(&filename_len, hdr + 38, 2);

        uint16_t funcname_len;
        memcpy(&funcname_len, hdr + 40, 2);

        uint32_t locals_len;
        memcpy(&locals_len, hdr + 42, 4);

        uint32_t expected_payload = (uint32_t)filename_len + funcname_len + locals_len;
        if (total_size != BINLOG_RECORD_HEADER_SIZE + expected_payload) {
            long skip = (long)(total_size - BINLOG_RECORD_HEADER_SIZE);
            if (skip > 0) fseek(fp, skip, SEEK_CUR);
            continue;
        }

        size_t payload_size = expected_payload;
        char *buf;
        if (payload_size <= sizeof(stack_buf)) {
            buf = stack_buf;
        } else {
            if (payload_size > heap_buf_size) {
                char *new_buf = (char *)realloc(heap_buf, payload_size + 1);
                if (!new_buf) {
                    error = 1;
                    break;
                }
                heap_buf = new_buf;
                heap_buf_size = payload_size + 1;
            }
            buf = heap_buf;
        }

        if (payload_size > 0) {
            if (fread(buf, payload_size, 1, fp) != 1) {
                break;
            }
        }

        /* Extract NUL-terminated strings */
        char fn_buf[PATH_MAX + 1];
        size_t fn_copy = filename_len < PATH_MAX ? filename_len : PATH_MAX;
        memcpy(fn_buf, buf, fn_copy);
        fn_buf[fn_copy] = '\0';

        char func_buf[1024];
        size_t func_copy = funcname_len < 1023 ? funcname_len : 1023;
        memcpy(func_buf, buf + filename_len, func_copy);
        func_buf[func_copy] = '\0';

        const char *locals_str = NULL;
        char locals_stack[8192];
        char *locals_heap = NULL;

        if (locals_len > 0) {
            char *locals_src = buf + filename_len + funcname_len;
            if (locals_len < sizeof(locals_stack)) {
                memcpy(locals_stack, locals_src, locals_len);
                locals_stack[locals_len] = '\0';
                locals_str = locals_stack;
            } else {
                locals_heap = (char *)malloc(locals_len + 1);
                if (locals_heap) {
                    memcpy(locals_heap, locals_src, locals_len);
                    locals_heap[locals_len] = '\0';
                    locals_str = locals_heap;
                }
            }
        }

        const char *event_str = unmap_event_type(event_type);

        /* Bind and insert */
        sqlite3_bind_text(stmt, 1, run_id, -1, SQLITE_STATIC);
        sqlite3_bind_int64(stmt, 2, (sqlite3_int64)seq);
        sqlite3_bind_double(stmt, 3, timestamp);
        sqlite3_bind_int(stmt, 4, line_no);
        sqlite3_bind_text(stmt, 5, fn_buf, -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(stmt, 6, func_buf, -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(stmt, 7, event_str, -1, SQLITE_STATIC);
        sqlite3_bind_int(stmt, 8, call_depth);
        if (locals_str) {
            sqlite3_bind_text(stmt, 9, locals_str, -1, SQLITE_TRANSIENT);
        } else {
            sqlite3_bind_null(stmt, 9);
        }
        sqlite3_bind_int64(stmt, 10, (sqlite3_int64)thread_id);
        sqlite3_bind_int(stmt, 11, is_coroutine);

        rc = sqlite3_step(stmt);
        sqlite3_reset(stmt);

        if (locals_heap) {
            free(locals_heap);
            locals_heap = NULL;
        }

        record_count++;

        /* Periodic commit every 10000 records to prevent WAL bloat */
        if ((record_count % 10000) == 0) {
            sqlite3_exec(sqldb, "COMMIT", NULL, NULL, NULL);
            sqlite3_exec(sqldb, "BEGIN", NULL, NULL, NULL);
        }
    }

    /* Final commit */
    sqlite3_exec(sqldb, "COMMIT", NULL, NULL, NULL);

    sqlite3_finalize(stmt);
    sqlite3_close(sqldb);

    if (heap_buf) free(heap_buf);
    fclose(fp);

    /* Delete binlog file on success */
    if (!error) {
#ifndef _WIN32
        unlink(binlog_path);
#else
        DeleteFileA(binlog_path);
#endif
    }

    return error ? -1 : 0;
}

/* ---- Python-facing functions ---- */

PyObject *pyttd_binlog_open(PyObject *self, PyObject *args) {
    (void)self;
    const char *db_path, *run_id;
    if (!PyArg_ParseTuple(args, "ss", &db_path, &run_id)) return NULL;

    if (binlog_open(db_path, run_id) != 0) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to open binlog");
        return NULL;
    }
    Py_RETURN_NONE;
}

PyObject *pyttd_binlog_load(PyObject *self, PyObject *args) {
    (void)self;
    const char *db_path;
    if (!PyArg_ParseTuple(args, "s", &db_path)) return NULL;

    if (binlog_load_into_sqlite(db_path) != 0) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to load binlog into SQLite");
        return NULL;
    }
    Py_RETURN_NONE;
}

PyObject *pyttd_binlog_set_size_limit(PyObject *self, PyObject *args) {
    (void)self;
    unsigned long long limit;
    if (!PyArg_ParseTuple(args, "K", &limit)) return NULL;
    binlog_set_size_limit((uint64_t)limit);
    Py_RETURN_NONE;
}
