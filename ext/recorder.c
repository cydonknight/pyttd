#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <frameobject.h>
#include <stdatomic.h>
#include <string.h>
#include <time.h>
#include <math.h>

#ifdef _WIN32
#include <windows.h>
#else
#include <pthread.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <fnmatch.h>
#endif

#include "platform.h"
#include "frame_event.h"
#include "recorder.h"
#include "ringbuf.h"
#include "iohook.h"
#include "binlog.h"

#ifdef PYTTD_HAS_FORK
#include "checkpoint.h"
#include "checkpoint_store.h"
#endif

/* ---- Version-gated macros for PEP 523 eval hook API ---- */

#if PY_VERSION_HEX >= 0x030F0000    /* 3.15+ */
  #define PYTTD_SET_EVAL_FUNC PyUnstable_InterpreterState_SetEvalFrameFunc
  #define PYTTD_GET_EVAL_FUNC PyUnstable_InterpreterState_GetEvalFrameFunc
#else                                 /* 3.12 - 3.14 */
  #define PYTTD_SET_EVAL_FUNC _PyInterpreterState_SetEvalFrameFunc
  #define PYTTD_GET_EVAL_FUNC _PyInterpreterState_GetEvalFrameFunc
#endif

/* Eval hook signature */
typedef PyObject *(*EvalFrameFunc)(PyThreadState *, struct _PyInterpreterFrame *, int);

/* ---- Maximum sizes ---- */
#define MAX_IGNORE_PATTERNS    64
#define MAX_REPR_LENGTH        256
#define FLUSH_BATCH_SIZE       4096

/* Coroutine/generator detection flags from co_flags */
#ifndef CO_COROUTINE
#define CO_COROUTINE        0x0100
#endif
#ifndef CO_GENERATOR
#define CO_GENERATOR        0x0020
#endif
#ifndef CO_ASYNC_GENERATOR
#define CO_ASYNC_GENERATOR  0x0200
#endif
#define PYTTD_CORO_FLAGS (CO_COROUTINE | CO_GENERATOR | CO_ASYNC_GENERATOR)

/* ---- Global State ---- */

_Atomic int g_recording = 0;
_Atomic int g_stop_requested = 0;
static _Atomic int g_interpreter_alive = 1;
_Atomic uint64_t g_sequence_counter = 0;
PYTTD_THREAD_LOCAL int g_call_depth = -1;
unsigned long g_main_thread_id = 0;
static EvalFrameFunc g_original_eval = NULL;
static PyObject *g_flush_callback = NULL;
static double g_start_time = 0.0;
static double g_stop_time = 0.0;
static RingbufStats g_saved_rb_stats = {0, 0};
PYTTD_THREAD_LOCAL int g_inside_repr = 0;
/* Most recent raise-site line observed by the trace function on this thread.
 * Updated in PyTrace_EXCEPTION; consumed by the eval hook when emitting an
 * exception_unwind event so the recorded line_no is the actual raise site
 * rather than the frame entry line. -1 means "not set". */
PYTTD_THREAD_LOCAL int g_last_exception_line = -1;

/* Perf: per-thread code object cache — avoids PyFrame_GetCode + UTF-8 extraction on cache hit */
PYTTD_THREAD_LOCAL PyFrameObject *g_cached_frame = NULL;
PYTTD_THREAD_LOCAL PyCodeObject *g_cached_code = NULL;
PYTTD_THREAD_LOCAL const char *g_cached_filename = NULL;
PYTTD_THREAD_LOCAL const char *g_cached_funcname = NULL;
PYTTD_THREAD_LOCAL int g_cached_is_coro = 0;

/* Perf: TLS thread ID cache — avoids PyThread_get_thread_ident() calls per event */
PYTTD_THREAD_LOCAL unsigned long g_my_thread_id = 0;

/* Perf: TLS cached timestamp — re-read every N LINE events within a frame */
PYTTD_THREAD_LOCAL double g_cached_timestamp = 0.0;
PYTTD_THREAD_LOCAL int g_timestamp_counter = 0;

/* Perf: per-thread locals sampling state */
PYTTD_THREAD_LOCAL int g_line_sample_counter = 0;
/* Opt 2: tracks whether the most recent LINE event in this frame serialized locals.
 * If true, RETURN only serializes __return__ (not full locals). */
PYTTD_THREAD_LOCAL int g_locals_captured_this_frame = 0;
/* Tracks whether any local in the current frame matched a secret pattern.
 * Sticky across LINE events within the same frame. When set, the optimized
 * serialize_return_only() path must emit "<redacted>" for __return__ because
 * container returns (tuples, lists, dicts) can leak redacted string values. */
PYTTD_THREAD_LOCAL int g_frame_had_redaction = 0;

/* Seen-lines cache: direct-mapped, detects first occurrence of a new line number.
 * Forces locals serialization on first visit to a new source line (handles
 * inlined comprehensions where 100+ events fire on the same line, then code
 * resumes on a different line with variables that must be captured). */
#define SEEN_LINES_SIZE 64
#define SEEN_LINES_MASK (SEEN_LINES_SIZE - 1)
PYTTD_THREAD_LOCAL int g_seen_lines[SEEN_LINES_SIZE];

static uint64_t g_max_frames = 0;  /* 0 = unlimited */
static int g_trace_installed_externally = 0;  /* set by trace_current_frame */

/* ---- Phase 2: Checkpoint/fast-forward state ---- */
static int g_fast_forward = 0;
int g_fast_forward_live = 0;  /* 1 = RESUME_LIVE mode (reinit recording on target) */
char g_resume_live_run_id[33] = {0};  /* Parent-provided run_id for branch (set by RESUME_LIVE handler) */
static uint64_t g_fast_forward_target = 0;
char g_recorder_db_path[1024] = {0};  /* Set by start_recording, used by checkpoint_child_go_live */
int g_server_socket_fd = -1;  /* TCP socket FD for handoff to checkpoint child */
PyObject *g_resume_live_callback = NULL;  /* Python callback for child bootstrap */
_Atomic uint64_t g_last_checkpoint_seq = 0;
static PyObject *g_checkpoint_callback = NULL;
static int g_checkpoint_interval = 0;
static PYTTD_THREAD_LOCAL int g_in_checkpoint = 0;  /* guard against recursive checkpoint triggering */
/* Item #6: per-thread "next checkpoint seq" cache.  Initialized to UINT64_MAX
 * so the fast path is a single compare that skips the checkpoint block
 * entirely until a checkpoint might need to fire. */
PYTTD_THREAD_LOCAL uint64_t g_my_next_checkpoint_seq = UINT64_MAX;

/* Child-only globals (set during child init) */
int g_cmd_fd = -1;
int g_result_fd = -1;
PyThreadState *g_saved_tstate = NULL;

/* ---- Stats ---- */
static _Atomic uint64_t g_flush_count = 0;
/* Issue 5: count of times a checkpoint trigger fired but was skipped because
 * multiple threads were active at the time (fork is unsafe in that case).
 * Surfaced via get_recording_stats() so the CLI can warn users that cold
 * navigation will be limited for this run. */
static _Atomic uint64_t g_checkpoints_skipped_threads = 0;

/* Issue 6: tracking for attach-mode checkpoints. When attach_mode is set,
 * synthesize_existing_stack() emits N synthetic call events at depths
 * matching the existing interpreter stack. A fork() before any "real"
 * frame has been recorded would have nothing useful to fast-forward into,
 * so checkpoints are gated until the sequence counter passes
 * g_attach_real_frames_start (which is the value of g_sequence_counter
 * just after synthesize_existing_stack() returns). g_attach_mode_active is
 * set in start_recording() and read by the checkpoint trigger blocks. */
static int g_attach_mode_active = 0;
static _Atomic uint64_t g_attach_real_frames_start = 0;
/* Warmup window after the synthesized prefix before checkpoints are
 * allowed. Avoids forking immediately on the first real frame. */
#define PYTTD_ATTACH_CHECKPOINT_WARMUP 10

/* ---- Ignore Filter ---- */
typedef struct {
    char *patterns[MAX_IGNORE_PATTERNS];
    int count;
} SubstringFilter;

typedef struct {
    char *entries[MAX_IGNORE_PATTERNS * 2];
    int count;
} ExactMatchSet;

static SubstringFilter g_dir_filter = {.count = 0};
static ExactMatchSet g_exact_filter = {.count = 0};

/* ---- Secrets Filter (Phase 8B) ---- */
static SubstringFilter g_secret_filter = {.count = 0};

/* ---- Include Filter (Phase 9B) ---- */
static SubstringFilter g_include_filter = {.count = 0};
static int g_include_mode = 0;  /* 1 if include patterns active */

/* ---- File Include Filter (P1-6) ---- */
static SubstringFilter g_file_include_filter = {.count = 0};
static int g_file_include_mode = 0;

/* ---- Exclude Filter (P1-6) ---- */
static SubstringFilter g_exclude_func_filter = {.count = 0};
static SubstringFilter g_exclude_file_filter = {.count = 0};
static int g_exclude_mode = 0;

/* ---- Exclude-Locals Filter (Item #5) ----
 * Files whose locals should NEVER be captured.  Events (call/line/return)
 * still fire so navigation works; only locals_json is suppressed. */
static SubstringFilter g_exclude_locals_filter = {.count = 0};
static int g_exclude_locals_mode = 0;
/* Max call depth beyond which locals are skipped (default: 20).
 * 0 disables the depth check. */
static int g_locals_max_depth = 20;
/* TLS: set on frame entry when the current frame should skip locals
 * capture entirely (module past warmup, over depth threshold, matched
 * by --exclude-locals). */
PYTTD_THREAD_LOCAL int g_cached_frame_locals_exempt = 0;

/* ---- Pre-interned Strings (perf: avoid per-event PyUnicode_FromString) ---- */
static PyObject *g_str_dunder_return = NULL;
static PyObject *g_str_dunder_exception = NULL;
static PyObject *g_key_sequence_no = NULL;
static PyObject *g_key_timestamp = NULL;
static PyObject *g_key_line_no = NULL;
static PyObject *g_key_filename = NULL;
static PyObject *g_key_function_name = NULL;
static PyObject *g_key_frame_event = NULL;
static PyObject *g_key_call_depth = NULL;
static PyObject *g_key_locals_snapshot = NULL;
static PyObject *g_key_thread_id = NULL;
static PyObject *g_key_is_coroutine = NULL;
/* Event type strings indexed by: c=0, l=1, r=2, e=3, E=4 (exception_unwind) */
static PyObject *g_evt_call = NULL;
static PyObject *g_evt_line = NULL;
static PyObject *g_evt_return = NULL;
static PyObject *g_evt_exception = NULL;
static PyObject *g_evt_exception_unwind = NULL;

static void intern_strings(void) {
    g_str_dunder_return = PyUnicode_InternFromString("__return__");
    g_str_dunder_exception = PyUnicode_InternFromString("__exception__");
    g_key_sequence_no = PyUnicode_InternFromString("sequence_no");
    g_key_timestamp = PyUnicode_InternFromString("timestamp");
    g_key_line_no = PyUnicode_InternFromString("line_no");
    g_key_filename = PyUnicode_InternFromString("filename");
    g_key_function_name = PyUnicode_InternFromString("function_name");
    g_key_frame_event = PyUnicode_InternFromString("frame_event");
    g_key_call_depth = PyUnicode_InternFromString("call_depth");
    g_key_locals_snapshot = PyUnicode_InternFromString("locals_snapshot");
    g_key_thread_id = PyUnicode_InternFromString("thread_id");
    g_key_is_coroutine = PyUnicode_InternFromString("is_coroutine");
    g_evt_call = PyUnicode_InternFromString("call");
    g_evt_line = PyUnicode_InternFromString("line");
    g_evt_return = PyUnicode_InternFromString("return");
    g_evt_exception = PyUnicode_InternFromString("exception");
    g_evt_exception_unwind = PyUnicode_InternFromString("exception_unwind");
}

static void release_interned_strings(void) {
    Py_XDECREF(g_str_dunder_return); g_str_dunder_return = NULL;
    Py_XDECREF(g_str_dunder_exception); g_str_dunder_exception = NULL;
    Py_XDECREF(g_key_sequence_no); g_key_sequence_no = NULL;
    Py_XDECREF(g_key_timestamp); g_key_timestamp = NULL;
    Py_XDECREF(g_key_line_no); g_key_line_no = NULL;
    Py_XDECREF(g_key_filename); g_key_filename = NULL;
    Py_XDECREF(g_key_function_name); g_key_function_name = NULL;
    Py_XDECREF(g_key_frame_event); g_key_frame_event = NULL;
    Py_XDECREF(g_key_call_depth); g_key_call_depth = NULL;
    Py_XDECREF(g_key_locals_snapshot); g_key_locals_snapshot = NULL;
    Py_XDECREF(g_key_thread_id); g_key_thread_id = NULL;
    Py_XDECREF(g_key_is_coroutine); g_key_is_coroutine = NULL;
    Py_XDECREF(g_evt_call); g_evt_call = NULL;
    Py_XDECREF(g_evt_line); g_evt_line = NULL;
    Py_XDECREF(g_evt_return); g_evt_return = NULL;
    Py_XDECREF(g_evt_exception); g_evt_exception = NULL;
    Py_XDECREF(g_evt_exception_unwind); g_evt_exception_unwind = NULL;
}

/* Map event_type C string to pre-interned PyObject (returns borrowed ref) */
static inline PyObject *event_type_to_pystr(const char *event_type) {
    switch (event_type[0]) {
    case 'c': return g_evt_call;
    case 'l': return g_evt_line;
    case 'r': return g_evt_return;
    case 'e':
        return (event_type[1] == 'x' && event_type[9] == '_')
            ? g_evt_exception_unwind : g_evt_exception;
    default: return NULL;
    }
}

/* ---- Filter Result Cache (perf: avoid repeated O(n) filter checks) ---- */
#define FILTER_CACHE_SIZE 512
#define FILTER_CACHE_MASK (FILTER_CACHE_SIZE - 1)

static struct {
    const char *filename;
    const char *funcname;
    int result;   /* 0=record, 1=skip, -1=invalid */
} g_filter_cache[FILTER_CACHE_SIZE];

static inline unsigned filter_cache_hash(const char *fn, const char *func) {
    return (unsigned)(((uintptr_t)fn ^ ((uintptr_t)func * 2654435761u)) >> 4) & FILTER_CACHE_MASK;
}

/* ---- Flush Thread ---- */
#ifdef _WIN32
static HANDLE g_flush_thread = NULL;
static HANDLE g_flush_mutex_win = NULL;
static HANDLE g_flush_event = NULL;
#else
pthread_t g_flush_thread;
int g_flush_thread_created = 0;
pthread_mutex_t g_flush_mutex = PTHREAD_MUTEX_INITIALIZER;
pthread_cond_t g_flush_cond = PTHREAD_COND_INITIALIZER;

/* Phase 2: Pre-fork synchronization condvars (non-static: accessed by checkpoint.c) */
pthread_cond_t g_pause_ack_cv = PTHREAD_COND_INITIALIZER;
pthread_cond_t g_resume_cv = PTHREAD_COND_INITIALIZER;
_Atomic int g_pause_requested = 0;
_Atomic int g_pause_acked = 0;
#endif
_Atomic int g_flush_stop = 0;
static int g_flush_interval_ms = 10;

/* ---- User Pause (live debugging) — "pause all" barrier ---- */
/* Distinct from pre-fork pause (g_pause_requested) which targets the flush thread.
 * User pause targets ALL recording threads — each parks at its next LINE boundary.
 * The RPC thread waits until all threads have parked (atomic barrier). */
_Atomic int g_user_pause_requested = 0;
_Atomic int g_user_paused = 0;               /* 1 when all threads parked (or timeout) */
_Atomic int g_user_pause_thread_count = 0;    /* Threads currently parked */
_Atomic int g_user_pause_expected = 0;        /* Total threads that must park */
/* Per-thread frame reference for the paused frame (TLS — each thread holds its own) */
PYTTD_THREAD_LOCAL PyObject *g_my_paused_frame = NULL;
/* Main thread's paused frame (for Phase 3 variable modification) */
static PyObject *g_main_paused_frame = NULL;
#ifdef _WIN32
static CRITICAL_SECTION g_user_pause_cs;
static CONDITION_VARIABLE g_user_pause_cv;
static CONDITION_VARIABLE g_user_pause_ack_cv;
static int g_user_pause_cs_initialized = 0;
#else
static pthread_mutex_t g_user_pause_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_user_pause_cv = PTHREAD_COND_INITIALIZER;
static pthread_cond_t g_user_pause_ack_cv = PTHREAD_COND_INITIALIZER;
#endif

/* ---- Flush-and-wait synchronization (for binlog snapshot on pause) ---- */
static _Atomic int g_flush_immediate = 0;
#ifndef _WIN32
static pthread_cond_t g_flush_done_cv = PTHREAD_COND_INITIALIZER;
#endif

/* ---- Phase 2 getter/setter ---- */

void recorder_set_fast_forward(int enabled, uint64_t target_seq) {
    g_fast_forward = enabled;
    g_fast_forward_target = target_seq;
}

void recorder_set_fast_forward_live(int enabled, uint64_t target_seq) {
    g_fast_forward = enabled;
    g_fast_forward_live = enabled;
    g_fast_forward_target = target_seq;
}

uint64_t recorder_get_sequence_counter(void) {
    return atomic_load_explicit(&g_sequence_counter, memory_order_relaxed);
}

int recorder_get_call_depth(void) {
    return g_call_depth;
}

/* ---- Monotonic Clock ---- */

static double get_monotonic_time(void) {
#ifdef _WIN32
    static LARGE_INTEGER freq = {0};
    LARGE_INTEGER counter;
    if (freq.QuadPart == 0) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart / (double)freq.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
#endif
}

/* ---- Ignore Filter Implementation ---- */

static void clear_filters(void) {
    for (int i = 0; i < g_dir_filter.count; i++) {
        free(g_dir_filter.patterns[i]);
        g_dir_filter.patterns[i] = NULL;
    }
    g_dir_filter.count = 0;

    for (int i = 0; i < g_exact_filter.count; i++) {
        free(g_exact_filter.entries[i]);
        g_exact_filter.entries[i] = NULL;
    }
    g_exact_filter.count = 0;
}

static int should_ignore(const char *filename, const char *funcname) {
    /* Fast check: CPython frozen modules (<frozen runpy>, <frozen importlib._bootstrap>,
     * etc.) are never user code. One prefix check catches them all. */
    if (strncmp(filename, "<frozen ", 8) == 0) {
        return 1;
    }

    /* Check substring patterns (directory patterns containing '/') */
    for (int i = 0; i < g_dir_filter.count; i++) {
        if (strstr(filename, g_dir_filter.patterns[i]) != NULL) {
            return 1;
        }
    }

    /* Check exact match patterns against basename of filename and function name */
    const char *basename = strrchr(filename, '/');
    if (!basename) basename = strrchr(filename, '\\');
    if (basename) basename++; else basename = filename;

    for (int i = 0; i < g_exact_filter.count; i++) {
        if (strcmp(basename, g_exact_filter.entries[i]) == 0 ||
            strcmp(funcname, g_exact_filter.entries[i]) == 0) {
            return 1;
        }
    }
    return 0;
}

/* ---- Secrets Filter (Phase 8B) ---- */

/* Case-insensitive substring search (portable — no strcasestr on Windows) */
static int ci_strstr(const char *haystack, const char *needle) {
    if (!haystack || !needle || !*needle) return 0;
    size_t nlen = strlen(needle);
    for (const char *h = haystack; *h; h++) {
        int match = 1;
        for (size_t i = 0; i < nlen; i++) {
            char hc = h[i];
            char nc = needle[i];
            if (!hc) { match = 0; break; }
            if (hc >= 'A' && hc <= 'Z') hc += 32;
            if (nc >= 'A' && nc <= 'Z') nc += 32;
            if (hc != nc) { match = 0; break; }
        }
        if (match) return 1;
    }
    return 0;
}

/* Check if character is a word boundary for secret pattern matching.
 * A boundary is: start/end of string, underscore, or transition between
 * alpha and non-alpha. This prevents "auth" from matching "authenticate"
 * while still matching "auth_token", "my_auth", or "AUTH". */
static int is_word_boundary(const char *str, size_t pos, size_t len) {
    if (pos == 0 || pos == len) return 1;
    char c = str[pos];
    char prev = str[pos - 1];
    if (c == '_' || prev == '_') return 1;
    return 0;
}

/* Item #8: case-insensitive trie over the secret-pattern alphabet (a-z + '_').
 * The old implementation was O(name_len * patterns_count * avg_pattern_len).
 * A single trie walk from each start position is O(name_len * max_depth),
 * which trims 3-8% of end-to-end cost with the default 13 patterns. */
#define PYTTD_TRIE_FANOUT 27

typedef struct PyttdTrieNode {
    struct PyttdTrieNode *children[PYTTD_TRIE_FANOUT];
    int is_terminal;
    int pattern_len;
} PyttdTrieNode;

static PyttdTrieNode *g_secret_trie_root = NULL;

static int secret_char_idx(char c) {
    if (c >= 'A' && c <= 'Z') c = (char)(c + 32);
    if (c >= 'a' && c <= 'z') return c - 'a';
    if (c == '_') return 26;
    return -1;
}

static PyttdTrieNode *secret_trie_new_node(void) {
    return (PyttdTrieNode *)calloc(1, sizeof(PyttdTrieNode));
}

static void secret_trie_free(PyttdTrieNode *n) {
    if (!n) return;
    for (int i = 0; i < PYTTD_TRIE_FANOUT; i++) secret_trie_free(n->children[i]);
    free(n);
}

static void secret_trie_insert(PyttdTrieNode *root, const char *pattern) {
    PyttdTrieNode *cur = root;
    int len = 0;
    for (const char *p = pattern; *p; p++) {
        int idx = secret_char_idx(*p);
        if (idx < 0) return;  /* pattern contains chars outside the alphabet */
        if (!cur->children[idx]) {
            cur->children[idx] = secret_trie_new_node();
            if (!cur->children[idx]) return;
        }
        cur = cur->children[idx];
        len++;
    }
    cur->is_terminal = 1;
    cur->pattern_len = len;
}

static void rebuild_secret_trie(void) {
    secret_trie_free(g_secret_trie_root);
    g_secret_trie_root = NULL;
    if (g_secret_filter.count == 0) return;
    g_secret_trie_root = secret_trie_new_node();
    if (!g_secret_trie_root) return;
    for (int i = 0; i < g_secret_filter.count; i++) {
        secret_trie_insert(g_secret_trie_root, g_secret_filter.patterns[i]);
    }
}

static int should_redact(const char *name) {
    if (!g_secret_trie_root) return 0;
    size_t name_len = strlen(name);
    for (size_t start = 0; start < name_len; start++) {
        /* Only check matches that begin on a word boundary — otherwise
         * "auth" inside "authenticate" would advance through the trie
         * and waste time only to be rejected by the end-boundary check. */
        if (!is_word_boundary(name, start, name_len)) continue;
        PyttdTrieNode *cur = g_secret_trie_root;
        for (size_t i = start; i < name_len; i++) {
            int idx = secret_char_idx(name[i]);
            if (idx < 0) break;
            cur = cur->children[idx];
            if (!cur) break;
            if (cur->is_terminal) {
                size_t end = i + 1;
                if (is_word_boundary(name, end, name_len)) return 1;
            }
        }
    }
    return 0;
}

static void clear_secret_filter(void) {
    for (int i = 0; i < g_secret_filter.count; i++) {
        free(g_secret_filter.patterns[i]);
        g_secret_filter.patterns[i] = NULL;
    }
    g_secret_filter.count = 0;
    secret_trie_free(g_secret_trie_root);
    g_secret_trie_root = NULL;
}

/* ---- Include Filter (Phase 9B) ---- */

static int has_glob_chars(const char *s) {
    for (; *s; s++) {
        if (*s == '*' || *s == '?' || *s == '[') return 1;
    }
    return 0;
}

/* Extract bare function name from a qualified name.
 * "main.<locals>.failing" -> "failing"
 * "OuterClass.method"     -> "method"
 * "simple"                -> "simple"
 */
static const char *bare_name(const char *qualname) {
    const char *last_dot = strrchr(qualname, '.');
    return last_dot ? last_dot + 1 : qualname;
}

static int should_include(const char *funcname) {
    if (!g_include_mode) return 1;
    /* <module> always included — top-level code must record */
    if (strcmp(funcname, "<module>") == 0) return 1;
    const char *bname = bare_name(funcname);
    for (int i = 0; i < g_include_filter.count; i++) {
#ifndef _WIN32
        if (fnmatch(g_include_filter.patterns[i], funcname, 0) == 0 ||
            fnmatch(g_include_filter.patterns[i], bname, 0) == 0) {
            return 1;
        }
#else
        if (strstr(funcname, g_include_filter.patterns[i]) != NULL) {
            return 1;
        }
#endif
    }
    return 0;
}

static void clear_include_filter(void) {
    for (int i = 0; i < g_include_filter.count; i++) {
        free(g_include_filter.patterns[i]);
        g_include_filter.patterns[i] = NULL;
    }
    g_include_filter.count = 0;
    g_include_mode = 0;
}

/* P1-6: File-path include filter */
static int should_include_file(const char *filename) {
    if (!g_file_include_mode) return 1;
    for (int i = 0; i < g_file_include_filter.count; i++) {
#ifndef _WIN32
        if (fnmatch(g_file_include_filter.patterns[i], filename, 0) == 0) {
            return 1;
        }
#else
        if (strstr(filename, g_file_include_filter.patterns[i]) != NULL) {
            return 1;
        }
#endif
    }
    return 0;
}

/* P1-6: Exclude filter (takes precedence over includes) */
static int should_exclude(const char *filename, const char *funcname) {
    if (!g_exclude_mode) return 0;
    /* For <module> frames, skip function-name exclusion but still check
     * file exclusion — users expect --exclude-file to suppress ALL frames
     * from that file, including module-level code. */
    if (strcmp(funcname, "<module>") != 0) {
        const char *bname = bare_name(funcname);
        for (int i = 0; i < g_exclude_func_filter.count; i++) {
#ifndef _WIN32
            if (fnmatch(g_exclude_func_filter.patterns[i], funcname, 0) == 0 ||
                fnmatch(g_exclude_func_filter.patterns[i], bname, 0) == 0) return 1;
#else
            if (strstr(funcname, g_exclude_func_filter.patterns[i]) != NULL) return 1;
#endif
        }
    }
    for (int i = 0; i < g_exclude_file_filter.count; i++) {
#ifndef _WIN32
        if (fnmatch(g_exclude_file_filter.patterns[i], filename, 0) == 0) return 1;
#else
        if (strstr(filename, g_exclude_file_filter.patterns[i]) != NULL) return 1;
#endif
    }
    return 0;
}

/* Item #5: File matches an --exclude-locals glob */
static int exclude_locals_matches(const char *filename) {
    if (!g_exclude_locals_mode) return 0;
    for (int i = 0; i < g_exclude_locals_filter.count; i++) {
#ifndef _WIN32
        if (fnmatch(g_exclude_locals_filter.patterns[i], filename, 0) == 0) {
            return 1;
        }
#else
        if (strstr(filename, g_exclude_locals_filter.patterns[i]) != NULL) {
            return 1;
        }
#endif
    }
    return 0;
}

/* ---- JSON Escaping ---- */

int recorder_json_escape_string(const char *src, char *dst, size_t dst_size) {
    if (!src) {
        if (dst_size < 5) return -1;
        memcpy(dst, "null", 5);
        return 4;
    }
    if (dst_size < 2) return -1;  /* need at least 1 char + NUL */
    size_t pos = 0;
    for (const char *p = src; *p; p++) {
        unsigned char c = (unsigned char)*p;
        if (c == '"') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = '"';
        } else if (c == '\\') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = '\\';
        } else if (c == '\n') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'n';
        } else if (c == '\r') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'r';
        } else if (c == '\t') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = 't';
        } else if (c == '\b') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'b';
        } else if (c == '\f') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'f';
        } else if (c < 0x20) {
            if (pos + 6 >= dst_size) return -1;
            pos += snprintf(dst + pos, dst_size - pos, "\\u%04x", c);
        } else {
            if (pos + 1 >= dst_size) return -1;
            dst[pos++] = (char)c;
        }
    }
    dst[pos] = '\0';
    return (int)pos;
}

/* Keep internal alias for backward compat within this file */
static int json_escape_string(const char *src, char *dst, size_t dst_size) {
    return recorder_json_escape_string(src, dst, dst_size);
}

/* ---- Locals Serialization ---- */

#define MAX_CHILDREN 50

/* Phase 10A: Detect if a value should be serialized as an expandable structure.
 * Only expand basic container types and user objects (not modules, types, functions, etc.) */
static int is_expandable(PyObject *value) {
    if (PyDict_Check(value) || PyList_Check(value) ||
        PyTuple_Check(value) || PySet_Check(value)) {
        return 1;
    }
    /* Skip modules, types, functions, methods, code objects */
    if (PyModule_Check(value) || PyType_Check(value) ||
        PyFunction_Check(value) || PyMethod_Check(value)) {
        return 0;
    }
    /* User objects with __dict__ (but not builtins) */
    if (PyObject_HasAttrString(value, "__dict__")) {
        PyObject *tp = (PyObject *)Py_TYPE(value);
        /* Skip types defined in C (builtins, extensions) — only expand Python classes */
        if (((PyTypeObject *)tp)->tp_flags & Py_TPFLAGS_HEAPTYPE) {
            return 1;
        }
    }
    /* User objects with __slots__ but no __dict__ (slots-only classes) */
    {
        PyObject *cls = (PyObject *)Py_TYPE(value);
        if (((PyTypeObject *)cls)->tp_flags & Py_TPFLAGS_HEAPTYPE &&
            PyObject_HasAttrString(cls, "__slots__")) {
            return 1;
        }
    }
    return 0;
}

/* Write JSON for a primitive value directly, including the surrounding
 * quotes.  None/bool/int/float reprs contain only characters that are
 * never escaped by json_escape_string, so the escape scan is redundant;
 * skipping it saves ~15-25% of primitive serialization time.
 *
 * Returns bytes written on success, -1 on buffer overflow or non-primitive
 * (caller must fall back to PyObject_Repr()). */
static int write_primitive_json(PyObject *value, char *buf, size_t buf_size) {
    if (value == Py_None) {
        if (buf_size < 6) return -1;
        memcpy(buf, "\"None\"", 6);
        return 6;
    }
    if (value == Py_True) {
        if (buf_size < 6) return -1;
        memcpy(buf, "\"True\"", 6);
        return 6;
    }
    if (value == Py_False) {
        if (buf_size < 7) return -1;
        memcpy(buf, "\"False\"", 7);
        return 7;
    }
    if (PyLong_CheckExact(value)) {
        int overflow;
        long long v = PyLong_AsLongLongAndOverflow(value, &overflow);
        if (overflow || PyErr_Occurred()) {
            PyErr_Clear();
            return -1;  /* bignum — fall through */
        }
        int n = snprintf(buf, buf_size, "\"%lld\"", v);
        if (n < 0 || (size_t)n >= buf_size) return -1;
        return n;
    }
    if (PyFloat_CheckExact(value)) {
        double d = PyFloat_AS_DOUBLE(value);
        if (isinf(d)) {
            const char *s = (d > 0) ? "\"inf\"" : "\"-inf\"";
            size_t len = (d > 0) ? 5 : 6;
            if (buf_size < len) return -1;
            memcpy(buf, s, len);
            return (int)len;
        }
        if (isnan(d)) {
            if (buf_size < 5) return -1;
            memcpy(buf, "\"nan\"", 5);
            return 5;
        }
        char *s = PyOS_double_to_string(d, 'r', 0, Py_DTSF_ADD_DOT_0, NULL);
        if (!s) return -1;
        size_t len = strlen(s);
        if (len + 2 >= buf_size) { PyMem_Free(s); return -1; }
        buf[0] = '"';
        memcpy(buf + 1, s, len);
        buf[len + 1] = '"';
        PyMem_Free(s);
        return (int)len + 2;
    }
    return -1;
}

/* Fast-path repr: format common primitive types directly into buf
 * without calling PyObject_Repr().  Returns buf (or a static literal)
 * on success, NULL if the value needs full PyObject_Repr() treatment. */
static const char *fast_repr(PyObject *value, char *buf, size_t buf_size) {
    if (value == Py_None)  return "None";
    if (value == Py_True)  return "True";
    if (value == Py_False) return "False";

    if (PyLong_Check(value)) {
        int overflow;
        long long v = PyLong_AsLongLongAndOverflow(value, &overflow);
        if (overflow || PyErr_Occurred()) {
            PyErr_Clear();
            return NULL;  /* bignum — fall through to PyObject_Repr */
        }
        int n = snprintf(buf, buf_size, "%lld", v);
        if (n < 0 || (size_t)n >= buf_size) return NULL;
        return buf;
    }

    if (PyFloat_Check(value)) {
        double d = PyFloat_AS_DOUBLE(value);
        if (isinf(d))  return (d > 0) ? "inf" : "-inf";
        if (isnan(d))  return "nan";
        /* Use CPython's dtoa for exact repr match */
        char *s = PyOS_double_to_string(d, 'r', 0, Py_DTSF_ADD_DOT_0, NULL);
        if (!s) return NULL;
        size_t len = strlen(s);
        if (len >= buf_size) { PyMem_Free(s); return NULL; }
        memcpy(buf, s, len + 1);
        PyMem_Free(s);
        return buf;
    }

    return NULL;
}

/* Get repr string for a child value, using fast path where possible.
 * Sets *repr_obj to non-NULL if a Python object was created (caller must XDECREF).
 * Returns the repr string, or NULL on failure. */
static inline const char *child_repr(PyObject *value, char *fast_buf, size_t fast_size,
                                      PyObject **repr_obj) {
    *repr_obj = NULL;
    const char *s = fast_repr(value, fast_buf, fast_size);
    if (s) return s;
    g_inside_repr = 1;
    *repr_obj = PyObject_Repr(value);
    g_inside_repr = 0;
    if (!*repr_obj) { PyErr_Clear(); return NULL; }
    s = PyUnicode_AsUTF8(*repr_obj);
    if (!s) { Py_DECREF(*repr_obj); *repr_obj = NULL; PyErr_Clear(); return NULL; }
    return s;
}

/* Write expandable structured JSON into buf at *pos.
 * Format: {"__type__":"dict","__len__":N,"__repr__":"...","__children__":[...]}
 * Returns 1 on success, 0 on buffer full (caller should fall back to flat repr). */
static int serialize_expandable_value(PyObject *value, char *buf, size_t buf_size,
                                       size_t *pos) {
    const char *type_name;
    Py_ssize_t length = 0;

    if (PyDict_Check(value)) {
        type_name = "dict";
        length = PyDict_Size(value);
    } else if (PyList_Check(value)) {
        type_name = "list";
        length = PyList_GET_SIZE(value);
    } else if (PyTuple_Check(value)) {
        type_name = "tuple";
        length = PyTuple_GET_SIZE(value);
    } else if (PySet_Check(value)) {
        type_name = "set";
        length = PySet_GET_SIZE(value);
    } else {
        type_name = "object";
    }

    /* Pre-scan: check if this container has any secret-named keys.
     * If so, use a scrubbed repr instead of the raw one to prevent
     * secret values from leaking through the container's repr string. */
    int has_secret_keys = 0;
    if (g_secret_filter.count > 0 && PyDict_Check(value)) {
        PyObject *dk, *dv;
        Py_ssize_t dpos = 0;
        while (PyDict_Next(value, &dpos, &dk, &dv)) {
            const char *dks = PyUnicode_AsUTF8(dk);
            if (dks && should_redact(dks)) { has_secret_keys = 1; break; }
        }
    }

    /* Get repr */
    const char *repr_str;
    PyObject *repr = NULL;
    char scrubbed_repr[128];
    if (has_secret_keys) {
        snprintf(scrubbed_repr, sizeof(scrubbed_repr),
                 "{...%zd items, secrets redacted...}", length);
        repr_str = scrubbed_repr;
    } else {
        g_inside_repr = 1;
        repr = PyObject_Repr(value);
        g_inside_repr = 0;
        if (!repr) { PyErr_Clear(); return 0; }
        repr_str = PyUnicode_AsUTF8(repr);
        if (!repr_str) { Py_DECREF(repr); PyErr_Clear(); return 0; }
    }

    char repr_truncated[MAX_REPR_LENGTH + 4];
    size_t repr_len = strlen(repr_str);
    if (repr_len > MAX_REPR_LENGTH) {
        memcpy(repr_truncated, repr_str, MAX_REPR_LENGTH);
        memcpy(repr_truncated + MAX_REPR_LENGTH, "...", 4);
        repr_str = repr_truncated;
    }

    /* Write: {"__type__":"<type>","__len__":<N>,"__repr__":"<repr>","__children__":[ */
    size_t needed = 80 + repr_len;
    if (*pos + needed >= buf_size) { Py_XDECREF(repr); return 0; }

    int n = snprintf(buf + *pos, buf_size - *pos,
                     "{\"__type__\": \"%s\", \"__len__\": %zd, \"__repr__\": \"",
                     type_name, length);
    if (n < 0 || (size_t)n >= buf_size - *pos) { Py_XDECREF(repr); return 0; }
    *pos += n;

    if (*pos + 30 >= buf_size) { Py_XDECREF(repr); return 0; }
    int esc_len = json_escape_string(repr_str, buf + *pos, buf_size - *pos - 30);
    Py_XDECREF(repr);
    if (esc_len < 0) return 0;
    *pos += esc_len;

    if (*pos + 21 >= buf_size) return 0;
    memcpy(buf + *pos, "\", \"__children__\": [", 20);
    *pos += 20;

    /* Serialize children (max MAX_CHILDREN, 1 level deep — children are flat repr) */
    int child_count = 0;
    int child_first = 1;

    int container_had_redaction = 0;
    if (PyDict_Check(value)) {
        PyObject *key, *val;
        Py_ssize_t dict_pos = 0;
        while (PyDict_Next(value, &dict_pos, &key, &val) && child_count < MAX_CHILDREN) {
            char fast_k[64], fast_v[64];
            PyObject *krepr, *vrepr;
            const char *ks = child_repr(key, fast_k, sizeof(fast_k), &krepr);
            const char *vs = child_repr(val, fast_v, sizeof(fast_v), &vrepr);
            if (!ks || !vs) {
                Py_XDECREF(krepr); Py_XDECREF(vrepr);
                continue;
            }
            /* Redact dict values whose key matches a secret pattern */
            int child_redacted = 0;
            if (g_secret_filter.count > 0 && should_redact(ks)) {
                vs = "<redacted>";
                container_had_redaction = 1;
                child_redacted = 1;
            }
            if (!child_first) {
                if (*pos + 2 >= buf_size) { Py_XDECREF(krepr); Py_XDECREF(vrepr); break; }
                buf[(*pos)++] = ',';
                buf[(*pos)++] = ' ';
            }
            child_first = 0;
            /* {"key":"k","value":"v","type":"t"} */
            const char *vtype = child_redacted ? "str" : val->ob_type->tp_name;
            if (*pos + 50 >= buf_size) { Py_XDECREF(krepr); Py_XDECREF(vrepr); break; }
            memcpy(buf + *pos, "{\"key\": \"", 9); *pos += 9;
            esc_len = json_escape_string(ks, buf + *pos, buf_size - *pos - 40);
            if (esc_len < 0) { Py_XDECREF(krepr); Py_XDECREF(vrepr); break; }
            *pos += esc_len;
            if (*pos + 44 >= buf_size) { Py_XDECREF(krepr); Py_XDECREF(vrepr); break; }
            memcpy(buf + *pos, "\", \"value\": \"", 13); *pos += 13;
            if (*pos + 30 >= buf_size) { Py_XDECREF(krepr); Py_XDECREF(vrepr); break; }
            esc_len = json_escape_string(vs, buf + *pos, buf_size - *pos - 30);
            Py_XDECREF(krepr); Py_XDECREF(vrepr);
            if (esc_len < 0) break;
            *pos += esc_len;
            n = snprintf(buf + *pos, buf_size - *pos, "\", \"type\": \"%s\"}", vtype);
            if (n < 0 || (size_t)n >= buf_size - *pos) break;
            *pos += n;
            child_count++;
        }
    } else if (PyList_Check(value) || PyTuple_Check(value)) {
        /* Check for NamedTuple: tuple subclass with a _fields attribute */
        PyObject *fields = NULL;
        int is_namedtuple = 0;
        if (PyTuple_Check(value) && PyObject_HasAttrString(value, "_fields")) {
            fields = PyObject_GetAttrString(value, "_fields");
            if (fields && PyTuple_Check(fields)) {
                is_namedtuple = 1;
            } else {
                Py_XDECREF(fields);
                fields = NULL;
                PyErr_Clear();
            }
        }
        Py_ssize_t len = PyList_Check(value) ? PyList_GET_SIZE(value) : PyTuple_GET_SIZE(value);
        Py_ssize_t limit = len < MAX_CHILDREN ? len : MAX_CHILDREN;
        for (Py_ssize_t i = 0; i < limit; i++) {
            PyObject *item = PyList_Check(value) ? PyList_GET_ITEM(value, i) : PyTuple_GET_ITEM(value, i);
            char fast_i[64];
            PyObject *irepr;
            const char *is = child_repr(item, fast_i, sizeof(fast_i), &irepr);
            if (!is) { continue; }
            if (!child_first) {
                if (*pos + 2 >= buf_size) { Py_XDECREF(irepr); break; }
                buf[(*pos)++] = ',';
                buf[(*pos)++] = ' ';
            }
            child_first = 0;
            const char *itype = item->ob_type->tp_name;
            /* Redact NamedTuple fields whose name matches a secret pattern */
            int nt_redacted = 0;
            /* Use field name for NamedTuples, numeric index otherwise */
            if (is_namedtuple && i < PyTuple_GET_SIZE(fields)) {
                const char *fname = PyUnicode_AsUTF8(PyTuple_GET_ITEM(fields, i));
                if (fname) {
                    if (g_secret_filter.count > 0 && should_redact(fname)) {
                        is = "<redacted>";
                        itype = "str";
                        container_had_redaction = 1;
                        nt_redacted = 1;
                    }
                    if (*pos + 50 >= buf_size) { Py_XDECREF(irepr); break; }
                    memcpy(buf + *pos, "{\"key\": \"", 9); *pos += 9;
                    esc_len = json_escape_string(fname, buf + *pos, buf_size - *pos - 40);
                    if (esc_len < 0) { Py_XDECREF(irepr); break; }
                    *pos += esc_len;
                    if (*pos + 16 >= buf_size) { Py_XDECREF(irepr); break; }
                    memcpy(buf + *pos, "\", \"value\": \"", 13); *pos += 13;
                } else {
                    PyErr_Clear();
                    n = snprintf(buf + *pos, buf_size - *pos, "{\"key\": \"%zd\", \"value\": \"", i);
                    if (n < 0 || (size_t)n >= buf_size - *pos) { Py_XDECREF(irepr); break; }
                    *pos += n;
                }
            } else {
                n = snprintf(buf + *pos, buf_size - *pos, "{\"key\": \"%zd\", \"value\": \"", i);
                if (n < 0 || (size_t)n >= buf_size - *pos) { Py_XDECREF(irepr); break; }
                *pos += n;
            }
            if (*pos + 30 >= buf_size) { Py_XDECREF(irepr); break; }
            esc_len = json_escape_string(is, buf + *pos, buf_size - *pos - 30);
            Py_XDECREF(irepr);
            if (esc_len < 0) break;
            *pos += esc_len;
            n = snprintf(buf + *pos, buf_size - *pos, "\", \"type\": \"%s\"}", itype);
            if (n < 0 || (size_t)n >= buf_size - *pos) break;
            *pos += n;
            child_count++;
        }
        Py_XDECREF(fields);
    } else if (PySet_Check(value)) {
        PyObject *iter = PyObject_GetIter(value);
        if (iter) {
            PyObject *item;
            int idx = 0;
            while ((item = PyIter_Next(iter)) && child_count < MAX_CHILDREN) {
                char fast_si[64];
                PyObject *irepr;
                const char *is = child_repr(item, fast_si, sizeof(fast_si), &irepr);
                if (!is) { Py_DECREF(item); continue; }
                if (!child_first) {
                    if (*pos + 2 >= buf_size) { Py_XDECREF(irepr); Py_DECREF(item); break; }
                    buf[(*pos)++] = ',';
                    buf[(*pos)++] = ' ';
                }
                child_first = 0;
                const char *itype = item->ob_type->tp_name;
                n = snprintf(buf + *pos, buf_size - *pos, "{\"key\": \"%d\", \"value\": \"", idx);
                if (n < 0 || (size_t)n >= buf_size - *pos) { Py_XDECREF(irepr); Py_DECREF(item); break; }
                *pos += n;
                if (*pos + 30 >= buf_size) { Py_XDECREF(irepr); Py_DECREF(item); break; }
                esc_len = json_escape_string(is, buf + *pos, buf_size - *pos - 30);
                Py_XDECREF(irepr);
                Py_DECREF(item);
                if (esc_len < 0) break;
                *pos += esc_len;
                n = snprintf(buf + *pos, buf_size - *pos, "\", \"type\": \"%s\"}", itype);
                if (n < 0 || (size_t)n >= buf_size - *pos) break;
                *pos += n;
                child_count++;
                idx++;
            }
            Py_DECREF(iter);
            if (PyErr_Occurred()) PyErr_Clear();
        } else {
            PyErr_Clear();
        }
    } else {
        /* Object: iterate __dict__ first, then __slots__ */
        PyObject *obj_dict = PyObject_GetAttrString(value, "__dict__");
        if (obj_dict && PyDict_Check(obj_dict)) {
            PyObject *key, *val;
            Py_ssize_t dict_pos = 0;
            while (PyDict_Next(obj_dict, &dict_pos, &key, &val) && child_count < MAX_CHILDREN) {
                const char *ks = PyUnicode_AsUTF8(key);
                if (!ks) { PyErr_Clear(); continue; }
                char fast_ov[64];
                PyObject *vrepr;
                const char *vs = child_repr(val, fast_ov, sizeof(fast_ov), &vrepr);
                if (!vs) { continue; }
                if (!child_first) {
                    if (*pos + 2 >= buf_size) { Py_XDECREF(vrepr); break; }
                    buf[(*pos)++] = ',';
                    buf[(*pos)++] = ' ';
                }
                child_first = 0;
                const char *vtype = val->ob_type->tp_name;
                if (*pos + 50 >= buf_size) { Py_XDECREF(vrepr); break; }
                memcpy(buf + *pos, "{\"key\": \"", 9); *pos += 9;
                esc_len = json_escape_string(ks, buf + *pos, buf_size - *pos - 40);
                if (esc_len < 0) { Py_XDECREF(vrepr); break; }
                *pos += esc_len;
                if (*pos + 44 >= buf_size) { Py_XDECREF(vrepr); break; }
                memcpy(buf + *pos, "\", \"value\": \"", 13); *pos += 13;
                if (*pos + 30 >= buf_size) { Py_XDECREF(vrepr); break; }
                esc_len = json_escape_string(vs, buf + *pos, buf_size - *pos - 30);
                Py_XDECREF(vrepr);
                if (esc_len < 0) break;
                *pos += esc_len;
                n = snprintf(buf + *pos, buf_size - *pos, "\", \"type\": \"%s\"}", vtype);
                if (n < 0 || (size_t)n >= buf_size - *pos) break;
                *pos += n;
                child_count++;
            }
        }
        Py_XDECREF(obj_dict);
        if (PyErr_Occurred()) PyErr_Clear();

        /* Also iterate __slots__ (handles slots-only classes and mixed dict+slots) */
        PyObject *cls = (PyObject *)Py_TYPE(value);
        PyObject *slots = PyObject_GetAttrString(cls, "__slots__");
        if (slots && (PyTuple_Check(slots) || PyList_Check(slots))) {
            Py_ssize_t nslots = PyTuple_Check(slots) ? PyTuple_GET_SIZE(slots) : PyList_GET_SIZE(slots);
            for (Py_ssize_t i = 0; i < nslots && child_count < MAX_CHILDREN; i++) {
                PyObject *slot_name = PyTuple_Check(slots) ? PyTuple_GET_ITEM(slots, i) : PyList_GET_ITEM(slots, i);
                const char *name_str = PyUnicode_AsUTF8(slot_name);
                if (!name_str) { PyErr_Clear(); continue; }
                PyObject *slot_val = PyObject_GetAttr(value, slot_name);
                if (!slot_val) { PyErr_Clear(); continue; }  /* unset slot */
                char fast_sv[64];
                PyObject *vrepr;
                const char *vs = child_repr(slot_val, fast_sv, sizeof(fast_sv), &vrepr);
                if (!vs) { Py_DECREF(slot_val); continue; }
                if (!child_first) {
                    if (*pos + 2 >= buf_size) { Py_XDECREF(vrepr); Py_DECREF(slot_val); break; }
                    buf[(*pos)++] = ',';
                    buf[(*pos)++] = ' ';
                }
                child_first = 0;
                const char *vtype = slot_val->ob_type->tp_name;
                if (*pos + 50 >= buf_size) { Py_XDECREF(vrepr); Py_DECREF(slot_val); break; }
                memcpy(buf + *pos, "{\"key\": \"", 9); *pos += 9;
                esc_len = json_escape_string(name_str, buf + *pos, buf_size - *pos - 40);
                if (esc_len < 0) { Py_XDECREF(vrepr); Py_DECREF(slot_val); break; }
                *pos += esc_len;
                if (*pos + 44 >= buf_size) { Py_XDECREF(vrepr); Py_DECREF(slot_val); break; }
                memcpy(buf + *pos, "\", \"value\": \"", 13); *pos += 13;
                if (*pos + 30 >= buf_size) { Py_XDECREF(vrepr); Py_DECREF(slot_val); break; }
                esc_len = json_escape_string(vs, buf + *pos, buf_size - *pos - 30);
                Py_XDECREF(vrepr);
                Py_DECREF(slot_val);
                if (esc_len < 0) break;
                *pos += esc_len;
                n = snprintf(buf + *pos, buf_size - *pos, "\", \"type\": \"%s\"}", vtype);
                if (n < 0 || (size_t)n >= buf_size - *pos) break;
                *pos += n;
                child_count++;
            }
        }
        Py_XDECREF(slots);
        if (PyErr_Occurred()) PyErr_Clear();
    }

    /* Close: ]} */
    if (*pos + 2 >= buf_size) return 0;
    buf[(*pos)++] = ']';
    buf[(*pos)++] = '}';
    return 1;
}

/* Module-level dunder variables that are expensive to repr and useless for debugging.
 * Skipping these saves ~100-150µs per locals serialization at module scope. */
static int should_skip_local(const char *key) {
    if (key[0] != '_' || key[1] != '_') return 0;
    /* Fast prefix check passed — compare against known dunders */
    return (strcmp(key, "__builtins__") == 0 ||
            strcmp(key, "__loader__") == 0 ||
            strcmp(key, "__spec__") == 0 ||
            strcmp(key, "__cached__") == 0 ||
            strcmp(key, "__annotations__") == 0);
}

static int serialize_one_local(const char *key_str, PyObject *value,
                               char *buf, size_t buf_size,
                               size_t *pos, int *first, size_t *last_complete_pos) {
    /* Skip expensive-to-repr module internals */
    if (should_skip_local(key_str)) return 1;

    /* Phase 8B: Secrets redaction — skip repr entirely for sensitive variables */
    if (should_redact(key_str)) {
        const char *redacted = "<redacted>";
        if (!*first) {
            if (*pos + 2 >= buf_size) return 0;
            buf[(*pos)++] = ',';
            buf[(*pos)++] = ' ';
        }
        *first = 0;
        /* Write "key": "<redacted>" */
        if (*pos + 12 >= buf_size) return 0;
        buf[(*pos)++] = '"';
        int esc_len = json_escape_string(key_str, buf + *pos, buf_size - *pos - 10);
        if (esc_len < 0) return 0;
        *pos += esc_len;
        if (*pos + 5 >= buf_size) return 0;
        buf[(*pos)++] = '"';
        buf[(*pos)++] = ':';
        buf[(*pos)++] = ' ';
        buf[(*pos)++] = '"';
        size_t rlen = strlen(redacted);
        if (*pos + rlen + 2 >= buf_size) return 0;
        memcpy(buf + *pos, redacted, rlen);
        *pos += rlen;
        buf[(*pos)++] = '"';
        *last_complete_pos = *pos;
        return 1;
    }

    /* Fast-path: format primitive types (None/bool/int/float) directly,
     * skipping PyObject_Repr(), is_expandable() (primitives never are),
     * and json_escape_string() for the value (primitive reprs are
     * always JSON-safe). */
    {
        /* Reserve enough headroom for separator, key-with-escapes, and
         * ": ".  Value writer needs up to ~40 bytes for float repr. */
        size_t guard = 50;  /* worst-case overhead besides escaped key */
        if (*pos + guard >= buf_size) return 0;
        size_t save_pos = *pos;
        int save_first = *first;
        if (!*first) {
            buf[(*pos)++] = ',';
            buf[(*pos)++] = ' ';
        }
        *first = 0;
        buf[(*pos)++] = '"';
        int esc_len = json_escape_string(key_str, buf + *pos, buf_size - *pos - 40);
        if (esc_len < 0) { *pos = save_pos; *first = save_first; return 0; }
        *pos += esc_len;
        if (*pos + 3 >= buf_size) { *pos = save_pos; *first = save_first; return 0; }
        buf[(*pos)++] = '"';
        buf[(*pos)++] = ':';
        buf[(*pos)++] = ' ';

        int prim_len = write_primitive_json(value, buf + *pos, buf_size - *pos);
        if (prim_len > 0) {
            *pos += prim_len;
            *last_complete_pos = *pos;
            return 1;
        }
        /* Not a primitive — rewind the "key": " prefix and fall through to
         * the expandable / repr paths below. */
        *pos = save_pos;
        *first = save_first;
    }

    /* Phase 10A: Try expandable serialization for container types */
    if (is_expandable(value)) {
        size_t save_pos = *pos;
        int save_first = *first;
        if (!*first) {
            if (*pos + 2 >= buf_size) return 0;
            buf[(*pos)++] = ',';
            buf[(*pos)++] = ' ';
        }
        *first = 0;
        /* Write "key": {structured...} */
        if (*pos + 12 >= buf_size) { *pos = save_pos; *first = save_first; goto flat_repr; }
        buf[(*pos)++] = '"';
        int esc_len = json_escape_string(key_str, buf + *pos, buf_size - *pos - 10);
        if (esc_len < 0) { *pos = save_pos; *first = save_first; goto flat_repr; }
        *pos += esc_len;
        if (*pos + 4 >= buf_size) { *pos = save_pos; *first = save_first; goto flat_repr; }
        buf[(*pos)++] = '"';
        buf[(*pos)++] = ':';
        buf[(*pos)++] = ' ';
        if (serialize_expandable_value(value, buf, buf_size, pos)) {
            *last_complete_pos = *pos;
            return 1;
        }
        /* Fall back to flat repr on buffer overflow */
        *pos = save_pos;
        *first = save_first;
    }

flat_repr:;
    g_inside_repr = 1;
    PyObject *repr = PyObject_Repr(value);
    g_inside_repr = 0;
    if (!repr) { PyErr_Clear(); return 1; }
    const char *repr_str = PyUnicode_AsUTF8(repr);
    if (!repr_str) { Py_DECREF(repr); PyErr_Clear(); return 1; }

    /* Truncate repr if too long */
    char repr_truncated[MAX_REPR_LENGTH + 4];
    size_t repr_len = strlen(repr_str);
    if (repr_len > MAX_REPR_LENGTH) {
        memcpy(repr_truncated, repr_str, MAX_REPR_LENGTH);
        memcpy(repr_truncated + MAX_REPR_LENGTH, "...", 4);
        repr_str = repr_truncated;
    }

    if (!*first) {
        if (*pos + 2 >= buf_size) { Py_DECREF(repr); return 0; }
        buf[(*pos)++] = ',';
        buf[(*pos)++] = ' ';
    }
    *first = 0;

    /* Write "key": "escaped_repr" */
    if (*pos + 12 >= buf_size) { Py_DECREF(repr); return 0; }
    buf[(*pos)++] = '"';
    int esc_len2 = json_escape_string(key_str, buf + *pos, buf_size - *pos - 10);
    if (esc_len2 < 0) { Py_DECREF(repr); return 0; }
    *pos += esc_len2;
    if (*pos + 5 >= buf_size) { Py_DECREF(repr); return 0; }
    buf[(*pos)++] = '"';
    buf[(*pos)++] = ':';
    buf[(*pos)++] = ' ';
    buf[(*pos)++] = '"';
    if (*pos + 4 >= buf_size) { Py_DECREF(repr); return 0; }
    int esc_len3 = json_escape_string(repr_str, buf + *pos, buf_size - *pos - 3);
    if (esc_len3 < 0) { Py_DECREF(repr); return 0; }
    *pos += esc_len3;
    if (*pos + 2 >= buf_size) { Py_DECREF(repr); return 0; }
    buf[(*pos)++] = '"';
    *last_complete_pos = *pos;
    Py_DECREF(repr);
    return 1;
}

const char *recorder_serialize_locals(PyObject *frame_obj, char *buf, size_t buf_size,
                                       PyObject *extra_key, PyObject *extra_val) {
    PyObject *locals = PyFrame_GetLocals((PyFrameObject *)frame_obj);
    if (!locals) {
        PyErr_Clear();
        return NULL;
    }

    size_t pos = 0;
    buf[pos++] = '{';

    int first = 1;
    size_t last_complete_pos = 1;
    int frame_had_redaction = 0;  /* Track for __return__ redaction */

#if PY_VERSION_HEX < 0x030D0000
    PyObject *key, *value;
    Py_ssize_t dict_pos = 0;
    while (PyDict_Next(locals, &dict_pos, &key, &value)) {
        const char *key_str = PyUnicode_AsUTF8(key);
        if (!key_str) { PyErr_Clear(); continue; }
        if (should_redact(key_str)) { frame_had_redaction = 1; g_frame_had_redaction = 1; }
        if (!serialize_one_local(key_str, value, buf, buf_size, &pos, &first, &last_complete_pos))
            break;
    }
#else
    PyObject *items = PyMapping_Items(locals);
    if (items) {
        Py_ssize_t n = PyList_GET_SIZE(items);
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *pair = PyList_GET_ITEM(items, i);
            PyObject *key = PyTuple_GET_ITEM(pair, 0);
            PyObject *value = PyTuple_GET_ITEM(pair, 1);
            const char *key_str = PyUnicode_AsUTF8(key);
            if (!key_str) { PyErr_Clear(); continue; }
            if (should_redact(key_str)) { frame_had_redaction = 1; g_frame_had_redaction = 1; }
            if (!serialize_one_local(key_str, value, buf, buf_size, &pos, &first, &last_complete_pos))
                break;
        }
        Py_DECREF(items);
    } else {
        PyErr_Clear();
    }
#endif

    /* Add extra key/value (e.g. __return__) if provided.
     * If any local in this frame was redacted, also redact the __return__
     * since containers can leak the redacted values through tuple/list returns. */
    if (extra_key && extra_val) {
        const char *ek = PyUnicode_AsUTF8(extra_key);
        if (ek) {
            if (frame_had_redaction && strcmp(ek, "__return__") == 0) {
                /* Write as redacted directly (bypass serialize_one_local) */
                if (!first && pos + 2 < buf_size) {
                    buf[pos++] = ',';
                    buf[pos++] = ' ';
                }
                first = 0;
                const char *redacted_template = "\"__return__\": \"<redacted>\"";
                size_t tlen = strlen(redacted_template);
                if (pos + tlen < buf_size) {
                    memcpy(buf + pos, redacted_template, tlen);
                    pos += tlen;
                    last_complete_pos = pos;
                }
            } else {
                if (!serialize_one_local(ek, extra_val, buf, buf_size, &pos, &first, &last_complete_pos)) {
                    /* buffer full — fall through to truncation handling */
                }
            }
        }
    }

    Py_DECREF(locals);

    if (pos + 1 >= buf_size) {
        pos = last_complete_pos;
        buf[pos++] = '}';
        buf[pos] = '\0';
        return buf;
    }
    buf[pos++] = '}';
    buf[pos] = '\0';
    return buf;
}

/* Keep static alias for internal use */
static const char *serialize_locals(PyObject *frame_obj, char *buf, size_t buf_size,
                                    PyObject *extra_key, PyObject *extra_val) {
    return recorder_serialize_locals(frame_obj, buf, buf_size, extra_key, extra_val);
}

/* Opt 2: serialize only the __return__ (or __exception__) extra key/value.
 * Produces: {"__return__": "<repr>"}.  Much cheaper than full serialize_locals()
 * which calls PyFrame_GetLocals() and iterates all variables.
 *
 * SECURITY: if the frame had any redacted local (tracked via sticky
 * g_frame_had_redaction flag set by serialize_locals), emit "<redacted>" for
 * __return__ — container returns like (password, api_key) would otherwise
 * leak the secret values through repr(). */
static const char *serialize_return_only(PyObject *extra_key, PyObject *extra_val,
                                          char *buf, size_t buf_size) {
    if (!extra_key || !extra_val) return NULL;
    const char *ek = PyUnicode_AsUTF8(extra_key);
    if (!ek) { PyErr_Clear(); return NULL; }

    /* If any local in this frame was redacted, emit redacted marker directly. */
    if (g_frame_had_redaction && strcmp(ek, "__return__") == 0) {
        const char *redacted = "{\"__return__\": \"<redacted>\"}";
        size_t rlen = strlen(redacted);
        if (rlen + 1 > buf_size) return NULL;
        memcpy(buf, redacted, rlen);
        buf[rlen] = '\0';
        return buf;
    }

    char fast_buf[64];
    PyObject *repr = NULL;
    const char *repr_str = fast_repr(extra_val, fast_buf, sizeof(fast_buf));
    if (!repr_str) {
        g_inside_repr = 1;
        repr = PyObject_Repr(extra_val);
        g_inside_repr = 0;
        if (!repr) { PyErr_Clear(); return NULL; }
        repr_str = PyUnicode_AsUTF8(repr);
        if (!repr_str) { Py_DECREF(repr); PyErr_Clear(); return NULL; }
    }

    /* Truncate if needed */
    char repr_truncated[MAX_REPR_LENGTH + 4];
    size_t repr_len = strlen(repr_str);
    if (repr_len > MAX_REPR_LENGTH) {
        memcpy(repr_truncated, repr_str, MAX_REPR_LENGTH);
        memcpy(repr_truncated + MAX_REPR_LENGTH, "...", 4);
        repr_str = repr_truncated;
    }

    size_t pos = 0;
    buf[pos++] = '{';
    buf[pos++] = '"';
    int esc_len = json_escape_string(ek, buf + pos, buf_size - pos - 20);
    if (esc_len < 0) { Py_XDECREF(repr); return NULL; }
    pos += esc_len;
    buf[pos++] = '"';
    buf[pos++] = ':';
    buf[pos++] = ' ';
    buf[pos++] = '"';
    int esc_len2 = json_escape_string(repr_str, buf + pos, buf_size - pos - 3);
    Py_XDECREF(repr);
    if (esc_len2 < 0) return NULL;
    pos += esc_len2;
    buf[pos++] = '"';
    buf[pos++] = '}';
    buf[pos] = '\0';
    return buf;
}

/* ---- Trace Function ---- */

char g_locals_buf[MAX_LOCALS_JSON_SIZE];

/* Forward declaration for fast-forward functions */
#ifdef PYTTD_HAS_FORK
static int pyttd_trace_func_fast_forward(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg);
#endif

int pyttd_trace_func(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg) {
    (void)obj;

#ifdef PYTTD_HAS_FORK
    if (g_fast_forward) {
        return pyttd_trace_func_fast_forward(obj, frame, what, arg);
    }
#endif

    if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) return 0;

    /* Check stop request in trace function (not just eval hook) so that
     * request_stop() can interrupt tight loops within a single frame.
     * Use atomic exchange to clear the flag — fire only once, so the
     * KeyboardInterrupt doesn't repeat in except/finally handlers.
     *
     * B3 fix: suppress during g_in_checkpoint. The trace function fires on
     * LINE events inside Python fork hooks (_prepareFork/_afterFork) called
     * by PyOS_BeforeFork. Raising KeyboardInterrupt there produces noisy
     * "RuntimeError: cannot release un-acquired lock" tracebacks. The stop
     * signal will be re-checked after the checkpoint completes. */
    if (what == PyTrace_LINE &&
        g_my_thread_id == g_main_thread_id &&
        !g_in_checkpoint &&
        atomic_exchange_explicit(&g_stop_requested, 0, memory_order_relaxed)) {
        PyErr_SetNone(PyExc_KeyboardInterrupt);
        return -1;
    }

    switch (what) {
    case PyTrace_CALL:
        /* Skip — eval hook already recorded the call event */
        return 0;

    case PyTrace_LINE: {
        int line_no = PyFrame_GetLineNumber(frame);

        /* Code object cache: avoid PyFrame_GetCode + UTF-8 extraction on cache hit */
        const char *filename;
        const char *funcname;
        int is_coro;
        int cache_hit = (frame == g_cached_frame);
        if (cache_hit) {
            filename = g_cached_filename;
            funcname = g_cached_funcname;
            is_coro = g_cached_is_coro;
        } else {
            PyCodeObject *code = PyFrame_GetCode(frame);
            filename = PyUnicode_AsUTF8(code->co_filename);
            funcname = PyUnicode_AsUTF8(code->co_qualname);
            if (!filename || !funcname) { PyErr_Clear(); Py_DECREF(code); return 0; }
            /* Filter check for frames not entered via eval hook (e.g. arm mode).
             * The eval hook normally prevents trace installation for filtered frames,
             * but trace_current_frame() bypasses this for the caller's frame. */
            if (should_ignore(filename, funcname) ||
                should_exclude(filename, funcname)) {
                Py_DECREF(code);
                return 0;
            }
            is_coro = (code->co_flags & PYTTD_CORO_FLAGS) ? 1 : 0;
            /* Update cache: release old code ref, hold new one */
            Py_XDECREF(g_cached_code);
            g_cached_frame = frame;
            g_cached_code = code;  /* transfer ownership of new ref */
            g_cached_filename = filename;
            g_cached_funcname = funcname;
            g_cached_is_coro = is_coro;
            g_line_sample_counter = 0;  /* new frame → reset sampling */
            g_locals_captured_this_frame = 0;
            g_frame_had_redaction = 0;  /* new frame → reset sticky redaction flag */
            memset(g_seen_lines, 0, sizeof(g_seen_lines));
            /* Item #5: decide once per frame whether to skip locals entirely.
             * Events still fire so stepping and breakpoints work; only the
             * locals_json payload is suppressed. Heuristics:
             *   - call depth past the configured threshold (library noise)
             *   - files matching --exclude-locals glob */
            g_cached_frame_locals_exempt = 0;
            if (g_locals_max_depth > 0 && g_call_depth > g_locals_max_depth) {
                g_cached_frame_locals_exempt = 1;
            } else if (exclude_locals_matches(filename)) {
                g_cached_frame_locals_exempt = 1;
            }
        }

        /* Locals sampling: serialize first WARMUP lines per frame, then every Nth.
         * Also force-serialize first occurrence of a new source line (handles
         * inlined comprehensions on 3.12+ where 100+ events fire on the same
         * line, followed by code on a new line that must capture variables). */
        const char *locals_json;
        #define LOCALS_WARMUP          16
        #define LOCALS_SAMPLE_INTERVAL 8
        g_line_sample_counter++;
        int first_visit = 0;
        if (g_line_sample_counter > LOCALS_WARMUP) {
            unsigned sl_idx = (unsigned)line_no & SEEN_LINES_MASK;
            if (g_seen_lines[sl_idx] != line_no) {
                g_seen_lines[sl_idx] = line_no;
                first_visit = 1;
            }
        }
        /* Adaptive sampling: faster backoff for long-running frames.
         * Locals in compute-heavy loops change little after the first few
         * iterations, so the aggressive curve preserves debugging fidelity
         * (first_visit still fires for newly-seen source lines) while
         * cutting serialization cost ~8x for tight loops. */
        int interval;
        if (g_line_sample_counter <= 64)         interval = LOCALS_SAMPLE_INTERVAL; /* 8 */
        else if (g_line_sample_counter <= 256)   interval = 32;
        else if (g_line_sample_counter <= 1024)  interval = 128;
        else if (g_line_sample_counter <= 4096)  interval = 512;
        else                                     interval = 1024;

        /* Item #5: exempt frames (deep library code, --exclude-locals matches)
         * skip locals entirely but still emit the event so timeline navigation
         * and breakpoints keep working.  The module-scope heuristic the plan
         * discusses is skipped here — typical test scripts do all their work
         * at module scope, and blanket-suppressing their locals breaks common
         * expectations.  Users can opt in with --exclude-locals if they want
         * to suppress a specific noisy file. */
        if (g_cached_frame_locals_exempt) {
            locals_json = NULL;
            g_locals_captured_this_frame = 0;
        } else if (g_line_sample_counter <= LOCALS_WARMUP
            || first_visit
            || (g_line_sample_counter % interval) == 0) {
            locals_json = serialize_locals((PyObject *)frame, g_locals_buf, sizeof(g_locals_buf), NULL, NULL);
            g_locals_captured_this_frame = 1;
        } else {
            locals_json = NULL;
            g_locals_captured_this_frame = 0;
        }

        /* Item 5: Batch timestamp reads — re-read every 64th LINE event within a frame.
         * 10ms-scale resolution is enough for the timeline view, and dropping
         * the rate from 12.5% of LINE events to ~1.5% saves measurable CPU. */
        if (!cache_hit || ++g_timestamp_counter >= 64) {
            g_cached_timestamp = get_monotonic_time() - g_start_time;
            g_timestamp_counter = 0;
        }

        FrameEvent event;
        event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        event.line_no = line_no;
        event.call_depth = g_call_depth;
        event.thread_id = g_my_thread_id;
        event.timestamp = g_cached_timestamp;
        event.event_type = "line";
        event.filename = filename;
        event.function_name = funcname;
        event.locals_json = locals_json;
        event.is_coroutine = is_coro;

        ringbuf_push(&event);

        /* User pause check — "pause all" barrier for live debugging.
         * Every recording thread parks here independently. The last thread
         * to park signals the RPC thread. Resume broadcasts to all. */
        if (atomic_load_explicit(&g_user_pause_requested, memory_order_acquire)) {
            /* Hold a strong reference to the paused frame (per-thread TLS) */
            Py_INCREF(frame);
            g_my_paused_frame = (PyObject *)frame;
            if (g_my_thread_id == g_main_thread_id) {
                g_main_paused_frame = (PyObject *)frame;
            }

            /* Park this thread — increment barrier counter */
            int parked = atomic_fetch_add_explicit(&g_user_pause_thread_count, 1,
                                                    memory_order_acq_rel) + 1;

            /* Signal RPC thread on FIRST park.  We don't wait for all threads because
             * ringbuf_thread_count() includes pre-allocated buffers for threads that
             * may not be executing user code.  The flush_and_wait mechanism drains
             * all pending events regardless.  Other threads park as they hit their
             * next LINE event (g_user_pause_requested stays set via the flag — each
             * thread reads it independently without clearing). */
            if (parked == 1) {
#ifdef _WIN32
                EnterCriticalSection(&g_user_pause_cs);
                atomic_store_explicit(&g_user_paused, 1, memory_order_release);
                WakeConditionVariable(&g_user_pause_ack_cv);
                LeaveCriticalSection(&g_user_pause_cs);
#else
                pthread_mutex_lock(&g_user_pause_mutex);
                atomic_store_explicit(&g_user_paused, 1, memory_order_release);
                pthread_cond_signal(&g_user_pause_ack_cv);
                pthread_mutex_unlock(&g_user_pause_mutex);
#endif
            }

            /* Release GIL and wait for resume broadcast */
            Py_BEGIN_ALLOW_THREADS
#ifdef _WIN32
            EnterCriticalSection(&g_user_pause_cs);
            while (atomic_load_explicit(&g_user_paused, memory_order_acquire)) {
                SleepConditionVariableCS(&g_user_pause_cv, &g_user_pause_cs, INFINITE);
            }
            LeaveCriticalSection(&g_user_pause_cs);
#else
            pthread_mutex_lock(&g_user_pause_mutex);
            while (atomic_load_explicit(&g_user_paused, memory_order_acquire)) {
                pthread_cond_wait(&g_user_pause_cv, &g_user_pause_mutex);
            }
            pthread_mutex_unlock(&g_user_pause_mutex);
#endif
            Py_END_ALLOW_THREADS

            /* Resumed — release per-thread paused frame reference */
            Py_XDECREF(g_my_paused_frame);
            g_my_paused_frame = NULL;
            if (g_my_thread_id == g_main_thread_id) {
                g_main_paused_frame = NULL;
            }
            atomic_fetch_sub_explicit(&g_user_pause_thread_count, 1, memory_order_relaxed);
        }

        /* P1-4: Auto-stop when max_frames reached (line events are most frequent).
         * Disable g_max_frames after first trigger to prevent cascading
         * KeyboardInterrupts when user code catches the first one. */
        if (g_max_frames > 0 && event.sequence_no >= g_max_frames) {
            g_max_frames = 0;
            atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
        }

        /* Checkpoint trigger — Item #6 fast path: a single TLS compare skips
         * the whole block until we're due for a checkpoint.  The slow path
         * (re-evaluate the full condition) runs only on hits. */
#ifdef PYTTD_HAS_FORK
        if (event.sequence_no >= g_my_next_checkpoint_seq) {
            uint64_t attach_floor = g_attach_mode_active
                ? atomic_load_explicit(&g_attach_real_frames_start, memory_order_relaxed)
                  + PYTTD_ATTACH_CHECKPOINT_WARMUP
                : 0;
            if (g_checkpoint_interval > 0 &&
                g_checkpoint_callback != NULL &&
                !g_in_checkpoint &&
                !atomic_load_explicit(&g_stop_requested, memory_order_relaxed) &&
                event.sequence_no > 0 &&
                event.sequence_no >= attach_floor) {
                if (ringbuf_thread_count() <= 1) {
                    atomic_store_explicit(&g_last_checkpoint_seq, event.sequence_no, memory_order_relaxed);
                    g_in_checkpoint = 1;
                    checkpoint_do_fork(event.sequence_no, g_checkpoint_callback);
                    g_in_checkpoint = 0;
                } else {
                    atomic_fetch_add_explicit(&g_checkpoints_skipped_threads, 1, memory_order_relaxed);
                    atomic_store_explicit(&g_last_checkpoint_seq, event.sequence_no, memory_order_relaxed);
                }
            }
            /* Advance the TLS deadline even on skip so we don't re-check
             * every event within a delta window. */
            g_my_next_checkpoint_seq = event.sequence_no + (uint64_t)g_checkpoint_interval;
        }
#endif

        /* Item 3: Throttle fill% check to every 64th event */
        if ((event.sequence_no & 63) == 0 && ringbuf_fill_percent() >= 75) {
#ifndef _WIN32
            pthread_cond_signal(&g_flush_cond);
#endif
        }
        return 0;
    }

    case PyTrace_RETURN: {
        if (arg == NULL) {
            /* Exception propagation — handled by eval hook as exception_unwind */
            return 0;
        }

        /* Use cache if available, otherwise get fresh code object */
        const char *ret_filename;
        const char *ret_funcname;
        int ret_is_coro;
        int ret_cache_hit = (frame == g_cached_frame);
        PyCodeObject *ret_code = NULL;
        if (ret_cache_hit) {
            ret_filename = g_cached_filename;
            ret_funcname = g_cached_funcname;
            ret_is_coro = g_cached_is_coro;
        } else {
            ret_code = PyFrame_GetCode(frame);
            ret_filename = PyUnicode_AsUTF8(ret_code->co_filename);
            ret_funcname = PyUnicode_AsUTF8(ret_code->co_qualname);
            if (!ret_filename || !ret_funcname) { PyErr_Clear(); Py_DECREF(ret_code); return 0; }
            if (should_ignore(ret_filename, ret_funcname) ||
                should_exclude(ret_filename, ret_funcname)) {
                Py_DECREF(ret_code);
                g_cached_frame = NULL;
                return 0;
            }
            ret_is_coro = (ret_code->co_flags & PYTTD_CORO_FLAGS) ? 1 : 0;
        }
        int line_no = PyFrame_GetLineNumber(frame);

        /* Opt 2: if the most recent LINE already captured locals, only serialize __return__.
         * Item #5: if the frame is exempt (matches --exclude-locals or beyond
         * locals_max_depth), emit NULL so the RETURN event doesn't leak
         * locals that the LINE events were told to skip. */
        const char *locals_json;
        if (ret_cache_hit && g_cached_frame_locals_exempt) {
            locals_json = NULL;
        } else if (g_locals_captured_this_frame) {
            locals_json = serialize_return_only(g_str_dunder_return, arg,
                                                g_locals_buf, sizeof(g_locals_buf));
        } else {
            locals_json = serialize_locals((PyObject *)frame, g_locals_buf,
                                           sizeof(g_locals_buf), g_str_dunder_return, arg);
        }

        FrameEvent event;
        event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        event.line_no = line_no;
        event.call_depth = g_call_depth;
        event.thread_id = g_my_thread_id;
        event.timestamp = get_monotonic_time() - g_start_time;  /* always fresh on RETURN */
        event.event_type = "return";
        event.filename = ret_filename;
        event.function_name = ret_funcname;
        event.locals_json = locals_json;
        event.is_coroutine = ret_is_coro;

        ringbuf_push(&event);
        if (ret_code) Py_DECREF(ret_code);

        if (g_max_frames > 0 && event.sequence_no >= g_max_frames) {
            g_max_frames = 0;
            atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
        }

        /* Invalidate cache — frame is being returned, CPython may recycle it */
        g_cached_frame = NULL;
        return 0;
    }

    case PyTrace_EXCEPTION: {
        /* Save active exception state — serialize_locals() has PyErr_Clear() paths
         * that can accidentally wipe the active exception on Python 3.12, breaking
         * exception_unwind detection in the eval hook's PyErr_Occurred() check */
        PyObject *save_type, *save_value, *save_tb;
        PyErr_Fetch(&save_type, &save_value, &save_tb);

        /* Use cache if available */
        const char *exc_filename;
        const char *exc_funcname;
        int exc_is_coro;
        PyCodeObject *exc_code = NULL;
        if (frame == g_cached_frame) {
            exc_filename = g_cached_filename;
            exc_funcname = g_cached_funcname;
            exc_is_coro = g_cached_is_coro;
        } else {
            exc_code = PyFrame_GetCode(frame);
            exc_filename = PyUnicode_AsUTF8(exc_code->co_filename);
            exc_funcname = PyUnicode_AsUTF8(exc_code->co_qualname);
            if (!exc_filename || !exc_funcname) {
                if (PyErr_Occurred()) PyErr_Clear();
                Py_DECREF(exc_code);
                goto exc_restore;
            }
            if (should_ignore(exc_filename, exc_funcname) ||
                should_exclude(exc_filename, exc_funcname)) {
                Py_DECREF(exc_code);
                goto exc_restore;
            }
            exc_is_coro = (exc_code->co_flags & PYTTD_CORO_FLAGS) ? 1 : 0;
        }
        int line_no = PyFrame_GetLineNumber(frame);
        /* Issue 3: stash the raise-site line so the eval hook can use it
         * when emitting exception_unwind. PyTrace_EXCEPTION fires on every
         * frame the exception passes through, so the most-recent value here
         * is the right line for the about-to-unwind frame. */
        g_last_exception_line = line_no;

        PyObject *exc_value = NULL;
        if (arg && PyTuple_Check(arg) && PyTuple_GET_SIZE(arg) >= 2) {
            exc_value = PyTuple_GET_ITEM(arg, 1);
        }
        const char *locals_json = serialize_locals((PyObject *)frame, g_locals_buf, sizeof(g_locals_buf), g_str_dunder_exception, exc_value);

        FrameEvent event;
        event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        event.line_no = line_no;
        event.call_depth = g_call_depth;
        event.thread_id = g_my_thread_id;
        event.timestamp = get_monotonic_time() - g_start_time;  /* always fresh on EXCEPTION */
        event.event_type = "exception";
        event.filename = exc_filename;
        event.function_name = exc_funcname;
        event.locals_json = locals_json;
        event.is_coroutine = exc_is_coro;

        ringbuf_push(&event);
        if (exc_code) Py_DECREF(exc_code);

        if (g_max_frames > 0 && event.sequence_no >= g_max_frames) {
            g_max_frames = 0;
            atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
        }

exc_restore:
        /* Discard any errors from recording, restore original exception */
        if (PyErr_Occurred()) PyErr_Clear();
        PyErr_Restore(save_type, save_value, save_tb);
        return 0;
    }

    default:
        return 0;
    }
}

/* ---- Fast-Forward Trace Function (Phase 2) ---- */

#ifdef PYTTD_HAS_FORK

/* Forward declarations */
int checkpoint_wait_for_command(int cmd_fd);
int serialize_target_state(int result_fd, int event_type, PyObject *trace_arg);
void checkpoint_child_go_live(int result_fd, int cmd_fd);

static int pyttd_trace_func_fast_forward(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg) {
    (void)obj;

    switch (what) {
    case PyTrace_CALL:
        /* Skip — eval hook already counted it */
        return 0;

    case PyTrace_LINE: {
        uint64_t seq = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        if (seq == g_fast_forward_target) {
            if (g_fast_forward_live) {
                checkpoint_child_go_live(g_result_fd, g_cmd_fd);
            } else {
                serialize_target_state(g_result_fd, -1, NULL);
                checkpoint_wait_for_command(g_cmd_fd);
            }
        }
        return 0;
    }

    case PyTrace_RETURN: {
        if (arg == NULL) return 0;  /* exception propagation */
        uint64_t seq = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        if (seq == g_fast_forward_target) {
            if (g_fast_forward_live) {
                checkpoint_child_go_live(g_result_fd, g_cmd_fd);
            } else {
                serialize_target_state(g_result_fd, PyTrace_RETURN, arg);
                checkpoint_wait_for_command(g_cmd_fd);
            }
        }
        return 0;
    }

    case PyTrace_EXCEPTION: {
        uint64_t seq = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        if (seq == g_fast_forward_target) {
            if (g_fast_forward_live) {
                checkpoint_child_go_live(g_result_fd, g_cmd_fd);
            } else {
                serialize_target_state(g_result_fd, PyTrace_EXCEPTION, arg);
                checkpoint_wait_for_command(g_cmd_fd);
            }
        }
        return 0;
    }

    default:
        return 0;
    }
}

#endif /* PYTTD_HAS_FORK */

/* ---- PEP 523 Frame Eval Hook ---- */

#ifdef PYTTD_HAS_FORK
/* Forward declaration */
static PyObject *pyttd_eval_hook_fast_forward(PyThreadState *tstate,
                                               struct _PyInterpreterFrame *iframe,
                                               int throwflag);
#endif

static PyObject *pyttd_eval_hook(PyThreadState *tstate,
                                  struct _PyInterpreterFrame *iframe,
                                  int throwflag) {
#ifdef PYTTD_HAS_FORK
    /* Fast-forward check BEFORE g_recording check */
    if (g_fast_forward) {
        return pyttd_eval_hook_fast_forward(tstate, iframe, throwflag);
    }
#endif

    /* Skip recording if not active */
    if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) {
        return g_original_eval(tstate, iframe, throwflag);
    }

    /* Inside repr/IO callback — suppress recording and remove inherited trace
     * to prevent trace function from recording line/return events in this frame */
    if (g_inside_repr) {
        Py_tracefunc saved_trace = tstate->c_tracefunc;
        PyObject *saved_traceobj = tstate->c_traceobj;
        Py_XINCREF(saved_traceobj);
        if (saved_trace) {
            PyEval_SetTrace(NULL, NULL);
        }
        PyObject *result = g_original_eval(tstate, iframe, throwflag);
        if (saved_trace) {
            PyEval_SetTrace(saved_trace, saved_traceobj);
        }
        Py_XDECREF(saved_traceobj);
        return result;
    }

    /* Check stop request — only interrupt the main thread, not the flush thread.
     * Use atomic exchange to clear the flag — fire only once.
     * B3: suppress during g_in_checkpoint to avoid raising KeyboardInterrupt
     * inside PyOS_BeforeFork's _prepareFork handler (logging module fork hooks). */
    if (g_my_thread_id == g_main_thread_id &&
        !g_in_checkpoint &&
        atomic_exchange_explicit(&g_stop_requested, 0, memory_order_relaxed)) {
        PyErr_SetNone(PyExc_KeyboardInterrupt);
        return NULL;
    }

    /* Get code object */
    PyCodeObject *code = PyUnstable_InterpreterFrame_GetCode(iframe);
    const char *filename = PyUnicode_AsUTF8(code->co_filename);
    const char *funcname = PyUnicode_AsUTF8(code->co_qualname);

    /* If UTF-8 conversion failed, treat as ignored and skip */
    if (!filename || !funcname) {
        PyErr_Clear();
        Py_DECREF(code);
        return g_original_eval(tstate, iframe, throwflag);
    }

    /* Unified filter check with cache (replaces 4 separate filter blocks) */
    {
        unsigned fc_idx = filter_cache_hash(filename, funcname);
        int skip;
        if (g_filter_cache[fc_idx].filename == filename &&
            g_filter_cache[fc_idx].funcname == funcname &&
            g_filter_cache[fc_idx].result >= 0) {
            skip = g_filter_cache[fc_idx].result;
        } else {
            skip = should_ignore(filename, funcname)
                || should_exclude(filename, funcname)
                || !should_include_file(filename)
                || !should_include(funcname);
            g_filter_cache[fc_idx].filename = filename;
            g_filter_cache[fc_idx].funcname = funcname;
            g_filter_cache[fc_idx].result = skip ? 1 : 0;
        }
        if (skip) {
            /* Save current trace, remove it, eval, restore.
             * Only call PyEval_SetTrace if trace is actually set
             * to avoid events counter overflow (Python 3.13+). */
            Py_tracefunc saved_trace = tstate->c_tracefunc;
            PyObject *saved_traceobj = tstate->c_traceobj;
            Py_XINCREF(saved_traceobj);
            if (saved_trace) {
                PyEval_SetTrace(NULL, NULL);
            }
            PyObject *result = g_original_eval(tstate, iframe, throwflag);
            if (saved_trace) {
                PyEval_SetTrace(saved_trace, saved_traceobj);
            }
            Py_XDECREF(saved_traceobj);
            Py_DECREF(code);
            return result;
        }
    }

    /* Ensure per-thread ring buffer exists (lazy allocation on first frame) */
    if (!ringbuf_get_thread_buffer()) {
        g_my_thread_id = PyThread_get_thread_ident();
        ringbuf_get_or_create(g_my_thread_id);
    }

    /* Record call event */
    g_call_depth++;
    int line_no = PyUnstable_InterpreterFrame_GetLine(iframe);
    int is_coro = (code->co_flags & PYTTD_CORO_FLAGS) ? 1 : 0;
    /* Issue 3: clear stale raise-site line before entering a new frame.
     * Defensive — the trace function should always set this fresh before
     * the eval hook reads it on unwind, but reset just in case a
     * PyTrace_EXCEPTION dispatch was missed for the previous frame. */
    g_last_exception_line = -1;

    /* Item 5: Always fresh timestamp on CALL (new frame) */
    g_cached_timestamp = get_monotonic_time() - g_start_time;
    g_timestamp_counter = 0;

    FrameEvent call_event;
    call_event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
    call_event.line_no = line_no;
    call_event.call_depth = g_call_depth;
    call_event.thread_id = g_my_thread_id;
    call_event.timestamp = g_cached_timestamp;
    call_event.event_type = "call";
    call_event.filename = filename;
    call_event.function_name = funcname;
    call_event.locals_json = NULL;
    call_event.is_coroutine = is_coro;

    ringbuf_push(&call_event);

    /* P1-4: Auto-stop when max_frames reached */
    if (g_max_frames > 0 && call_event.sequence_no >= g_max_frames) {
        g_max_frames = 0;
        atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
    }

    /* Checkpoint trigger — Item #6 TLS fast path (matches pyttd_trace_func). */
#ifdef PYTTD_HAS_FORK
    if (call_event.sequence_no >= g_my_next_checkpoint_seq) {
        uint64_t attach_floor_call = g_attach_mode_active
            ? atomic_load_explicit(&g_attach_real_frames_start, memory_order_relaxed)
              + PYTTD_ATTACH_CHECKPOINT_WARMUP
            : 0;
        if (g_checkpoint_interval > 0 &&
            g_checkpoint_callback != NULL &&
            !g_in_checkpoint &&
            !atomic_load_explicit(&g_stop_requested, memory_order_relaxed) &&
            call_event.sequence_no > 0 &&
            call_event.sequence_no >= attach_floor_call) {
            if (ringbuf_thread_count() <= 1) {
                atomic_store_explicit(&g_last_checkpoint_seq, call_event.sequence_no, memory_order_relaxed);
                g_in_checkpoint = 1;
                checkpoint_do_fork(call_event.sequence_no, g_checkpoint_callback);
                g_in_checkpoint = 0;
            } else {
                atomic_fetch_add_explicit(&g_checkpoints_skipped_threads, 1, memory_order_relaxed);
                atomic_store_explicit(&g_last_checkpoint_seq, call_event.sequence_no, memory_order_relaxed);
            }
        }
        g_my_next_checkpoint_seq = call_event.sequence_no + (uint64_t)g_checkpoint_interval;
    }
#endif

    /* Save current trace function, install ours.
     * Optimization: skip if our trace is already installed to avoid
     * PyEval_SetTrace's internal events counter overflow (Python 3.13+). */
    Py_tracefunc saved_trace = tstate->c_tracefunc;
    PyObject *saved_traceobj = tstate->c_traceobj;
    Py_XINCREF(saved_traceobj);

    int trace_changed = (saved_trace != (Py_tracefunc)pyttd_trace_func);
    if (trace_changed) {
        PyEval_SetTrace(pyttd_trace_func, NULL);
    }

    /* Call original eval */
    PyObject *result = g_original_eval(tstate, iframe, throwflag);

    /* Check for exception_unwind (eval returned NULL with exception) */
    if (result == NULL && PyErr_Occurred()) {
        /* Issue 3: prefer the raise-site line stashed by the trace function's
         * PyTrace_EXCEPTION handler. Falls back to the frame entry line only
         * if no raise-site line was observed (e.g. trace was suppressed). */
        int unwind_line = (g_last_exception_line > 0) ? g_last_exception_line : line_no;
        FrameEvent unwind_event;
        unwind_event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        unwind_event.line_no = unwind_line;
        unwind_event.call_depth = g_call_depth;
        unwind_event.thread_id = g_my_thread_id;
        unwind_event.timestamp = get_monotonic_time() - g_start_time;  /* always fresh on exception_unwind */
        unwind_event.event_type = "exception_unwind";
        unwind_event.filename = filename;
        unwind_event.function_name = funcname;
        unwind_event.locals_json = NULL;
        unwind_event.is_coroutine = is_coro;

        ringbuf_push(&unwind_event);
    }

    /* Decrement call_depth */
    g_call_depth--;

    /* Restore previous trace function (only if we changed it) */
    if (trace_changed) {
        if (saved_trace) {
            PyEval_SetTrace(saved_trace, saved_traceobj);
        } else {
            PyEval_SetTrace(NULL, NULL);
        }
    }
    Py_XDECREF(saved_traceobj);
    Py_DECREF(code);

    return result;
}

/* ---- Fast-Forward Eval Hook (Phase 2) ---- */

#ifdef PYTTD_HAS_FORK

void serialize_error_result(int result_fd, const char *error_code, uint64_t last_seq);

static PyObject *pyttd_eval_hook_fast_forward(PyThreadState *tstate,
                                               struct _PyInterpreterFrame *iframe,
                                               int throwflag) {
    /* Get code object */
    PyCodeObject *code = PyUnstable_InterpreterFrame_GetCode(iframe);
    const char *filename = PyUnicode_AsUTF8(code->co_filename);
    const char *funcname = PyUnicode_AsUTF8(code->co_qualname);

    if (!filename || !funcname) {
        PyErr_Clear();
        Py_DECREF(code);
        return g_original_eval(tstate, iframe, throwflag);
    }

    /* Cache-backed filter check (same cache as main eval hook) */
    {
        unsigned fc_idx = filter_cache_hash(filename, funcname);
        int skip;
        if (g_filter_cache[fc_idx].filename == filename &&
            g_filter_cache[fc_idx].funcname == funcname &&
            g_filter_cache[fc_idx].result >= 0) {
            skip = g_filter_cache[fc_idx].result;
        } else {
            skip = should_ignore(filename, funcname)
                || should_exclude(filename, funcname)
                || !should_include_file(filename)
                || !should_include(funcname);
            g_filter_cache[fc_idx].filename = filename;
            g_filter_cache[fc_idx].funcname = funcname;
            g_filter_cache[fc_idx].result = skip ? 1 : 0;
        }
        if (skip) {
            Py_tracefunc saved_trace = tstate->c_tracefunc;
            PyObject *saved_traceobj = tstate->c_traceobj;
            Py_XINCREF(saved_traceobj);
            if (saved_trace) {
                PyEval_SetTrace(NULL, NULL);
            }
            PyObject *result = g_original_eval(tstate, iframe, throwflag);
            if (saved_trace) {
                PyEval_SetTrace(saved_trace, saved_traceobj);
            }
            Py_XDECREF(saved_traceobj);
            Py_DECREF(code);
            return result;
        }
    }

    /* Check thread */
    if (PyThread_get_thread_ident() != g_main_thread_id) {
        Py_DECREF(code);
        return g_original_eval(tstate, iframe, throwflag);
    }

    /* Count call event (no serialization/ringbuf in fast-forward) */
    g_call_depth++;
    uint64_t call_seq = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);

    /* Check if target reached at call event */
    if (call_seq == g_fast_forward_target) {
        if (g_fast_forward_live) {
            checkpoint_child_go_live(g_result_fd, g_cmd_fd);
        } else {
            serialize_target_state(g_result_fd, -1, NULL);
            checkpoint_wait_for_command(g_cmd_fd);
        }
    }

    /* Install trace function for line/return/exception counting.
     * Optimization: skip if our trace is already installed. */
    Py_tracefunc saved_trace = tstate->c_tracefunc;
    PyObject *saved_traceobj = tstate->c_traceobj;
    Py_XINCREF(saved_traceobj);

    if (saved_trace != (Py_tracefunc)pyttd_trace_func_fast_forward) {
        PyEval_SetTrace(pyttd_trace_func_fast_forward, NULL);
    }

    /* Call original eval */
    PyObject *result = g_original_eval(tstate, iframe, throwflag);

    /* Check for exception_unwind */
    if (result == NULL && PyErr_Occurred()) {
        uint64_t unwind_seq = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        if (unwind_seq == g_fast_forward_target) {
            if (g_fast_forward_live) {
                checkpoint_child_go_live(g_result_fd, g_cmd_fd);
            } else {
                serialize_target_state(g_result_fd, -1, NULL);
                checkpoint_wait_for_command(g_cmd_fd);
            }
        }
    }

    g_call_depth--;

    /* Restore trace */
    if (saved_trace != (Py_tracefunc)pyttd_trace_func_fast_forward) {
        if (saved_trace) {
            PyEval_SetTrace(saved_trace, saved_traceobj);
        } else {
            PyEval_SetTrace(NULL, NULL);
        }
    }
    Py_XDECREF(saved_traceobj);
    Py_DECREF(code);

    /* Check if script ended during fast-forward */
    if (g_fast_forward && g_call_depth < 0) {
        uint64_t cur_seq = atomic_load_explicit(&g_sequence_counter, memory_order_relaxed);
        if (cur_seq <= g_fast_forward_target) {
            serialize_error_result(g_result_fd, "target_seq_unreachable",
                                   cur_seq);
        }
        /* Permanent loop — only DIE exits (via _exit) */
        while (1) {
            checkpoint_wait_for_command(g_cmd_fd);
            serialize_error_result(g_result_fd, "script_completed",
                                   atomic_load_explicit(&g_sequence_counter, memory_order_relaxed));
        }
    }

    return result;
}

#endif /* PYTTD_HAS_FORK */

/* ---- Flush Thread ---- */

static void flush_one_buffer(ThreadRingBuffer *rb) {
    static FrameEvent batch[FLUSH_BATCH_SIZE];
    uint32_t count = 0;

    ringbuf_pop_batch_from(rb, batch, FLUSH_BATCH_SIZE, &count);
    if (count == 0) {
        if (rb->orphaned) {
            rb->initialized = 0;  /* mark for cleanup */
        }
        return;
    }

    if (!atomic_load_explicit(&g_interpreter_alive, memory_order_relaxed)) {
        return;
    }

    /* Pool swap must be serialized with producer (which holds GIL during push).
     * Brief GIL acquisition — just one integer write, not 40K object allocations. */
    PyGILState_STATE gstate = PyGILState_Ensure();
    ringbuf_pool_swap_for(rb);
    PyGILState_Release(gstate);

    /* Write to binary log — no GIL, no SQLite, just fwrite */
    if (binlog_write_batch(batch, count) < 0) {
        /* I/O error writing binlog — stop recording to prevent silent data loss */
        atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
    }
    atomic_fetch_add_explicit(&g_flush_count, 1, memory_order_relaxed);

    /* Consumer pool reset — flush thread owns consumer pool exclusively. */
    ringbuf_pool_reset_consumer_for(rb);
}

static void flush_batch(void) {
    /* Iterate all per-thread ring buffers */
    for (ThreadRingBuffer *rb = ringbuf_get_head(); rb != NULL; rb = rb->next) {
        if (!rb->initialized) continue;
        flush_one_buffer(rb);
    }
}

#ifdef _WIN32
static DWORD WINAPI flush_thread_func(LPVOID arg) {
    (void)arg;
    while (!atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) {
        Sleep(g_flush_interval_ms);
        if (atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) break;
        flush_batch();
    }
    /* Final flush — drain ALL remaining events (ring buffer may hold more
     * than FLUSH_BATCH_SIZE if the script was faster than the flush interval) */
    do {
        flush_batch();
    } while (ringbuf_any_pending());

    /* Close flush thread's C-level SQLite connection (no GIL needed) */
    binlog_close();
    return 0;
}
#else
void *flush_thread_func(void *arg) {
    (void)arg;
    while (!atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        long add_ns = (long)g_flush_interval_ms * 1000000L;
        ts.tv_sec += add_ns / 1000000000L;
        ts.tv_nsec += add_ns % 1000000000L;
        if (ts.tv_nsec >= 1000000000L) {
            ts.tv_sec += 1;
            ts.tv_nsec -= 1000000000L;
        }

        pthread_mutex_lock(&g_flush_mutex);
        pthread_cond_timedwait(&g_flush_cond, &g_flush_mutex, &ts);
        pthread_mutex_unlock(&g_flush_mutex);

        if (atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) break;
        flush_batch();

        /* Flush-and-wait: immediate drain requested by pause handler */
        if (atomic_exchange_explicit(&g_flush_immediate, 0, memory_order_acq_rel)) {
            /* One more drain to catch anything added since last batch */
            flush_batch();
            pthread_mutex_lock(&g_flush_mutex);
            pthread_cond_signal(&g_flush_done_cv);
            pthread_mutex_unlock(&g_flush_mutex);
        }

        /* Phase 2: Pre-fork synchronization — pause if requested */
        if (atomic_load_explicit(&g_pause_requested, memory_order_acquire)) {
            pthread_mutex_lock(&g_flush_mutex);
            atomic_store(&g_pause_acked, 1);
            pthread_cond_signal(&g_pause_ack_cv);
            while (atomic_load(&g_pause_requested)) {
                pthread_cond_wait(&g_resume_cv, &g_flush_mutex);
            }
            pthread_mutex_unlock(&g_flush_mutex);
        }
    }
    /* Final flush — drain ALL remaining events (ring buffer may hold more
     * than FLUSH_BATCH_SIZE if the script was faster than the flush interval) */
    do {
        flush_batch();
    } while (ringbuf_any_pending());

    /* Close flush thread's C-level SQLite connection (no GIL needed) */
    binlog_close();
    return NULL;
}
#endif

/* ---- Attach Mode: Synthesize existing stack ---- */

#define MAX_SYNTH_DEPTH 256

void synthesize_existing_stack(void) {
    PyFrameObject *current = PyEval_GetFrame();
    if (!current) return;
    /* Hold an extra ref to keep the current frame alive during the stack
     * walk. Without this, DECREF'ing intermediate back-frames during
     * traversal can trigger GC cycles that invalidate the current frame. */
    Py_INCREF(current);

    struct {
        PyFrameObject *frame;
        PyCodeObject *code;
        const char *filename;
        const char *funcname;
    } stack[MAX_SYNTH_DEPTH];
    int count = 0;

    PyFrameObject *f = current;
    while (f && count < MAX_SYNTH_DEPTH) {
        PyCodeObject *code = PyFrame_GetCode(f);
        const char *filename = PyUnicode_AsUTF8(code->co_filename);
        const char *funcname = PyUnicode_AsUTF8(code->co_qualname);

        if (!filename || !funcname) {
            PyErr_Clear();
            Py_DECREF(code);
            PyFrameObject *back = PyFrame_GetBack(f);
            if (f != current) Py_DECREF(f);
            f = back;
            continue;
        }

        if (should_ignore(filename, funcname) ||
            should_exclude(filename, funcname) ||
            !should_include_file(filename) ||
            !should_include(funcname)) {
            Py_DECREF(code);
            PyFrameObject *back = PyFrame_GetBack(f);
            if (f != current) Py_DECREF(f);
            f = back;
            continue;
        }

        stack[count].frame = f;
        stack[count].code = code;
        stack[count].filename = filename;
        stack[count].funcname = funcname;
        count++;

        PyFrameObject *back = PyFrame_GetBack(f);
        f = back;
    }
    if (f && f != current) {
        Py_DECREF(f);
    }

    for (int i = count - 1; i >= 0; i--) {
        g_call_depth++;
        int line_no = PyFrame_GetLineNumber(stack[i].frame);
        int is_coro = (stack[i].code->co_flags & PYTTD_CORO_FLAGS) ? 1 : 0;

        const char *locals_json = serialize_locals(
            (PyObject *)stack[i].frame, g_locals_buf, sizeof(g_locals_buf),
            NULL, NULL);

        FrameEvent event;
        event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        event.line_no = line_no;
        event.call_depth = g_call_depth;
        event.thread_id = PyThread_get_thread_ident();
        event.timestamp = get_monotonic_time() - g_start_time;
        event.event_type = "call";
        event.filename = stack[i].filename;
        event.function_name = stack[i].funcname;
        event.locals_json = locals_json;
        event.is_coroutine = is_coro;

        ringbuf_push(&event);

        Py_DECREF(stack[i].code);
    }

    Py_DECREF(current);
    for (int i = 0; i < count; i++) {
        /* current's ref was already released above; non-current frames
         * own a new ref from PyFrame_GetBack() that we release here. */
        if (stack[i].frame != current)
            Py_DECREF(stack[i].frame);
    }
}

/* ---- Python-Facing Functions ---- */

PyObject *pyttd_start_recording(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;

    if (atomic_load_explicit(&g_recording, memory_order_relaxed)) {
        PyErr_SetString(PyExc_RuntimeError, "Recording is already active");
        return NULL;
    }

    static char *kwlist[] = {"flush_callback", "buffer_size", "flush_interval_ms",
                             "checkpoint_callback", "checkpoint_interval",
                             "io_flush_callback", "io_replay_loader",
                             "attach_mode", "db_path",
                             "resume_live_callback", NULL};
    PyObject *callback = NULL;
    int buffer_size = PYTTD_DEFAULT_CAPACITY;
    int flush_interval_ms = 10;
    PyObject *checkpoint_cb = NULL;
    int checkpoint_interval = 0;
    PyObject *io_flush_cb = NULL;
    PyObject *io_replay_loader = NULL;
    int attach_mode = 0;
    const char *db_path_arg = NULL;
    PyObject *resume_live_cb = NULL;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|iiOiOOpsO", kwlist,
                                      &callback, &buffer_size, &flush_interval_ms,
                                      &checkpoint_cb, &checkpoint_interval,
                                      &io_flush_cb, &io_replay_loader,
                                      &attach_mode, &db_path_arg,
                                      &resume_live_cb)) {
        return NULL;
    }

    if (!PyCallable_Check(callback)) {
        PyErr_SetString(PyExc_TypeError, "flush_callback must be callable");
        return NULL;
    }

    /* Store flush interval */
    g_flush_interval_ms = flush_interval_ms > 0 ? flush_interval_ms : 10;

    /* Validate buffer_size */
    if (buffer_size < 0 || (buffer_size > 0 && buffer_size < 64)) {
        PyErr_SetString(PyExc_ValueError, "buffer_size must be 0 (default) or >= 64");
        return NULL;
    }

    /* Validate checkpoint_interval */
    if (checkpoint_interval < 0) {
        PyErr_SetString(PyExc_ValueError, "checkpoint_interval must be >= 0");
        return NULL;
    }

    /* Initialize ring buffer system (per-thread buffers) */
    if (ringbuf_system_init((uint32_t)buffer_size) != PYTTD_RINGBUF_OK) {
        PyErr_SetString(PyExc_MemoryError, "Failed to initialize ring buffer");
        return NULL;
    }

    /* Reset counters */
    atomic_store_explicit(&g_sequence_counter, 0, memory_order_relaxed);
    g_call_depth = -1;  /* TLS — main thread */
    atomic_store_explicit(&g_flush_count, 0, memory_order_relaxed);
    g_stop_time = 0.0;
    g_saved_rb_stats = (RingbufStats){0, 0};
    g_inside_repr = 0;
    g_last_exception_line = -1;
    g_cached_frame = NULL;
    Py_XDECREF(g_cached_code); g_cached_code = NULL;
    g_cached_filename = NULL;
    g_cached_funcname = NULL;
    g_cached_is_coro = 0;
    g_line_sample_counter = 0;
    g_locals_captured_this_frame = 0;
    g_frame_had_redaction = 0;
    memset(g_seen_lines, 0, sizeof(g_seen_lines));
    g_cached_timestamp = 0.0;
    g_timestamp_counter = 0;
    g_fast_forward = 0;
    g_fast_forward_live = 0;
    g_fast_forward_target = 0;
    g_server_socket_fd = -1;
    g_max_frames = 0;
    g_trace_installed_externally = 0;
    atomic_store_explicit(&g_last_checkpoint_seq, 0, memory_order_relaxed);
    atomic_store_explicit(&g_checkpoints_skipped_threads, 0, memory_order_relaxed);
    atomic_store_explicit(&g_attach_real_frames_start, 0, memory_order_relaxed);
    g_attach_mode_active = 0;
    g_in_checkpoint = 0;
    g_cmd_fd = -1;
    g_result_fd = -1;
    g_saved_tstate = NULL;
    atomic_store_explicit(&g_stop_requested, 0, memory_order_relaxed);
    atomic_store_explicit(&g_flush_stop, 0, memory_order_relaxed);
    atomic_store_explicit(&g_interpreter_alive, 1, memory_order_relaxed);
#ifndef _WIN32
    atomic_store(&g_pause_requested, 0);
    atomic_store(&g_pause_acked, 0);
#endif

    /* Reset user pause state */
    atomic_store_explicit(&g_user_pause_requested, 0, memory_order_relaxed);
    atomic_store_explicit(&g_user_paused, 0, memory_order_relaxed);
    atomic_store_explicit(&g_user_pause_thread_count, 0, memory_order_relaxed);
    atomic_store_explicit(&g_user_pause_expected, 0, memory_order_relaxed);
    g_my_paused_frame = NULL;
    g_main_paused_frame = NULL;
    atomic_store_explicit(&g_flush_immediate, 0, memory_order_relaxed);
#ifdef _WIN32
    if (!g_user_pause_cs_initialized) {
        InitializeCriticalSection(&g_user_pause_cs);
        InitializeConditionVariable(&g_user_pause_cv);
        InitializeConditionVariable(&g_user_pause_ack_cv);
        g_user_pause_cs_initialized = 1;
    }
#endif

    /* Pre-intern constant strings for flush and trace hot paths */
    intern_strings();

    /* Initialize filter cache */
    memset(g_filter_cache, 0xFF, sizeof(g_filter_cache));

    /* Save flush callback */
    g_flush_callback = callback;
    Py_INCREF(g_flush_callback);

    /* Save checkpoint callback */
    g_checkpoint_callback = NULL;
    g_checkpoint_interval = 0;
    if (checkpoint_cb && checkpoint_cb != Py_None && PyCallable_Check(checkpoint_cb)) {
        g_checkpoint_callback = checkpoint_cb;
        Py_INCREF(g_checkpoint_callback);
    }
    if (checkpoint_interval > 0) {
        g_checkpoint_interval = checkpoint_interval;
    }
    /* Item #6: seed the per-thread checkpoint deadline.  UINT64_MAX when
     * checkpointing is disabled so the fast path short-circuits without
     * any further work. */
    if (g_checkpoint_interval > 0) {
        g_my_next_checkpoint_seq = (uint64_t)g_checkpoint_interval;
    } else {
        g_my_next_checkpoint_seq = UINT64_MAX;
    }

    /* Save db_path for checkpoint_child_go_live */
    if (db_path_arg) {
        strncpy(g_recorder_db_path, db_path_arg, sizeof(g_recorder_db_path) - 1);
        g_recorder_db_path[sizeof(g_recorder_db_path) - 1] = '\0';
    }

    /* Save resume_live callback for child bootstrap */
    Py_XDECREF(g_resume_live_callback);
    g_resume_live_callback = NULL;
    if (resume_live_cb && resume_live_cb != Py_None && PyCallable_Check(resume_live_cb)) {
        g_resume_live_callback = resume_live_cb;
        Py_INCREF(g_resume_live_callback);
    }

    /* Initialize checkpoint store */
#ifdef PYTTD_HAS_FORK
    checkpoint_store_init();
    /* Ignore SIGPIPE — writing to checkpoint pipes whose child has died
     * (read end closed) would otherwise terminate the process.
     * With SIG_IGN, write() returns -1/EPIPE which callers handle gracefully. */
    signal(SIGPIPE, SIG_IGN);
#endif

    /* Record main thread ID and start time */
    g_main_thread_id = PyThread_get_thread_ident();
    g_my_thread_id = g_main_thread_id;
    g_start_time = get_monotonic_time();

    /* Pre-create main thread's ring buffer */
    ringbuf_get_or_create(g_main_thread_id);

    /* Set environment variable so user scripts can detect recording mode */
#ifdef _WIN32
    _putenv_s("PYTTD_RECORDING", "1");
#else
    setenv("PYTTD_RECORDING", "1", 1);
#endif

    /* Install I/O hooks (before recording starts, non-fatal on failure) */
    if (io_flush_cb && io_flush_cb != Py_None) {
        install_io_hooks_internal(io_flush_cb, io_replay_loader);
    }

    /* Issue 6: Attach-mode checkpoint policy is now driven by the caller.
     * If checkpoint_interval == 0 (the default for arm()), no callback is
     * registered above and no checkpoints fire. Callers that explicitly
     * opt in (arm(checkpoints=True)) pass a non-zero interval and accept
     * the additional risk of forking from a process whose pre-arm state
     * is unknown to pyttd. The C-side gate g_attach_real_frames_start
     * still prevents forking until the synthesized prefix is past us. */
    if (attach_mode) {
        g_attach_mode_active = 1;
    }

    /* Save original eval function and install our hook */
    PyInterpreterState *interp = PyInterpreterState_Get();
    g_original_eval = PYTTD_GET_EVAL_FUNC(interp);
    atomic_store_explicit(&g_recording, 1, memory_order_relaxed);

    /* Attach mode: synthesize call events for existing stack frames
     * BEFORE installing the eval hook */
    if (attach_mode) {
        synthesize_existing_stack();
        /* Issue 6: stamp the boundary between the synthesized prefix and
         * the first "real" frame the eval hook will see. Checkpoints
         * gated below this seq + warmup are skipped. */
        atomic_store_explicit(&g_attach_real_frames_start,
                              atomic_load_explicit(&g_sequence_counter, memory_order_relaxed),
                              memory_order_relaxed);
    }

    PYTTD_SET_EVAL_FUNC(interp, pyttd_eval_hook);

    /* Start flush thread */
#ifdef _WIN32
    g_flush_thread = CreateThread(NULL, 0, flush_thread_func, NULL, 0, NULL);
    if (!g_flush_thread) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to create flush thread");
        atomic_store_explicit(&g_recording, 0, memory_order_relaxed);
        PYTTD_SET_EVAL_FUNC(interp, g_original_eval);
        remove_io_hooks_internal();
        ringbuf_destroy();
        Py_DECREF(g_flush_callback);
        g_flush_callback = NULL;
        Py_XDECREF(g_checkpoint_callback);
        g_checkpoint_callback = NULL;
        return NULL;
    }
#else
    g_flush_thread_created = 0;
    if (pthread_create(&g_flush_thread, NULL, flush_thread_func, NULL) != 0) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to create flush thread");
        atomic_store_explicit(&g_recording, 0, memory_order_relaxed);
        PYTTD_SET_EVAL_FUNC(interp, g_original_eval);
        remove_io_hooks_internal();
        ringbuf_destroy();
        Py_DECREF(g_flush_callback);
        g_flush_callback = NULL;
        Py_XDECREF(g_checkpoint_callback);
        g_checkpoint_callback = NULL;
        return NULL;
    }
    g_flush_thread_created = 1;
#endif
    Py_RETURN_NONE;
}

PyObject *pyttd_stop_recording(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;

    if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) {
        Py_RETURN_NONE;
    }

    g_stop_time = get_monotonic_time();
    atomic_store_explicit(&g_recording, 0, memory_order_relaxed);

    /* If user-paused, resume ALL recording threads so they can unwind cleanly */
    if (atomic_load_explicit(&g_user_paused, memory_order_relaxed) ||
        atomic_load_explicit(&g_user_pause_thread_count, memory_order_relaxed) > 0) {
        atomic_store_explicit(&g_user_pause_requested, 0, memory_order_release);
        atomic_store_explicit(&g_user_paused, 0, memory_order_release);
#ifdef _WIN32
        EnterCriticalSection(&g_user_pause_cs);
        WakeAllConditionVariable(&g_user_pause_cv);
        LeaveCriticalSection(&g_user_pause_cs);
#else
        pthread_mutex_lock(&g_user_pause_mutex);
        pthread_cond_broadcast(&g_user_pause_cv);
        pthread_mutex_unlock(&g_user_pause_mutex);
#endif
    }
    atomic_store_explicit(&g_user_pause_requested, 0, memory_order_relaxed);

    /* Release code object cache */
    g_cached_frame = NULL;
    Py_XDECREF(g_cached_code); g_cached_code = NULL;
    g_cached_filename = NULL;
    g_cached_funcname = NULL;
    g_last_exception_line = -1;
    atomic_store_explicit(&g_stop_requested, 0, memory_order_relaxed);

    /* Clear recording environment variable */
#ifdef _WIN32
    _putenv_s("PYTTD_RECORDING", "");
#else
    unsetenv("PYTTD_RECORDING");
#endif

    /* Remove I/O hooks before flushing */
    remove_io_hooks_internal();

    /* Restore original eval function */
    PyInterpreterState *interp = PyInterpreterState_Get();
    PYTTD_SET_EVAL_FUNC(interp, g_original_eval);

    /* Remove trace function.  If trace_current_frame was used (attach mode),
     * PyEval_SetTrace modified internal monitoring state that persists even
     * after clearing.  Install a dummy trace then clear again to force the
     * monitoring system to fully reset. */
    PyEval_SetTrace(NULL, NULL);
    if (g_trace_installed_externally) {
        /* Re-install and remove to force full monitoring cleanup on 3.12+ */
        PyEval_SetTrace((Py_tracefunc)pyttd_trace_func, Py_None);
        PyEval_SetTrace(NULL, NULL);
        g_trace_installed_externally = 0;
    }

    /* Stop flush thread */
    atomic_store_explicit(&g_flush_stop, 1, memory_order_relaxed);
#ifdef _WIN32
    if (g_flush_thread) {
        Py_BEGIN_ALLOW_THREADS
        WaitForSingleObject(g_flush_thread, INFINITE);
        Py_END_ALLOW_THREADS
        CloseHandle(g_flush_thread);
        g_flush_thread = NULL;
    }
#else
    if (g_flush_thread_created) {
        /* If flush thread is paused for fork, resume it first */
        pthread_mutex_lock(&g_flush_mutex);
        atomic_store(&g_pause_requested, 0);
        pthread_cond_signal(&g_resume_cv);
        pthread_cond_signal(&g_flush_cond);
        pthread_mutex_unlock(&g_flush_mutex);

        Py_BEGIN_ALLOW_THREADS
        pthread_join(g_flush_thread, NULL);
        Py_END_ALLOW_THREADS
        g_flush_thread_created = 0;
    }
#endif

    /* Save ring buffer stats before destroy */
    g_saved_rb_stats = ringbuf_get_stats();

    /* Destroy ring buffer system */
    ringbuf_system_destroy();

    /* Release pre-interned strings */
    release_interned_strings();

    /* Release callbacks — NOTE: do NOT kill checkpoint children here,
     * they're needed for replay */
    Py_XDECREF(g_flush_callback);
    g_flush_callback = NULL;
    Py_XDECREF(g_checkpoint_callback);
    g_checkpoint_callback = NULL;
    g_checkpoint_interval = 0;

    Py_RETURN_NONE;
}

PyObject *pyttd_get_recording_stats(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;

    RingbufStats rb_stats = g_saved_rb_stats;
    double elapsed = (g_stop_time > 0.0 ? g_stop_time : get_monotonic_time()) - g_start_time;

    PyObject *dict = PyDict_New();
    if (!dict) return NULL;

    PyObject *fc = PyLong_FromUnsignedLongLong(atomic_load_explicit(&g_sequence_counter, memory_order_relaxed));
    PyObject *df = PyLong_FromUnsignedLongLong(rb_stats.dropped_frames);
    PyObject *et = PyFloat_FromDouble(elapsed);
    PyObject *flc = PyLong_FromUnsignedLongLong(atomic_load_explicit(&g_flush_count, memory_order_relaxed));
    PyObject *po = PyLong_FromUnsignedLongLong(rb_stats.pool_overflows);

    if (!fc || !df || !et || !flc || !po) {
        Py_XDECREF(fc); Py_XDECREF(df); Py_XDECREF(et);
        Py_XDECREF(flc); Py_XDECREF(po);
        Py_DECREF(dict);
        return PyErr_NoMemory();
    }

    PyDict_SetItemString(dict, "frame_count", fc);
    PyDict_SetItemString(dict, "dropped_frames", df);
    PyDict_SetItemString(dict, "elapsed_time", et);
    PyDict_SetItemString(dict, "flush_count", flc);
    PyDict_SetItemString(dict, "pool_overflows", po);

    Py_DECREF(fc); Py_DECREF(df); Py_DECREF(et);
    Py_DECREF(flc); Py_DECREF(po);

    /* Checkpoint memory stats */
    checkpoint_store_refresh_rss();
    PyObject *cp_count = PyLong_FromLong(checkpoint_store_count());
    PyObject *cp_mem = PyLong_FromUnsignedLongLong(checkpoint_store_total_rss());
    if (cp_count && cp_mem) {
        PyDict_SetItemString(dict, "checkpoint_count", cp_count);
        PyDict_SetItemString(dict, "checkpoint_memory_bytes", cp_mem);
    }
    Py_XDECREF(cp_count);
    Py_XDECREF(cp_mem);

    /* Issue 5: surface multi-thread checkpoint skip count so the CLI/UI can
     * warn that cold navigation is limited for this run. */
    PyObject *cp_skipped = PyLong_FromUnsignedLongLong(
        atomic_load_explicit(&g_checkpoints_skipped_threads, memory_order_relaxed));
    if (cp_skipped) {
        PyDict_SetItemString(dict, "checkpoints_skipped_threads", cp_skipped);
        Py_DECREF(cp_skipped);
    }

    /* Issue 6: surface the synthesized-stack boundary so attach-mode runs
     * can persist it and the replay layer can refuse cold jumps before it. */
    PyObject *attach_seq = PyLong_FromUnsignedLongLong(
        atomic_load_explicit(&g_attach_real_frames_start, memory_order_relaxed));
    if (attach_seq) {
        PyDict_SetItemString(dict, "attach_safe_seq", attach_seq);
        Py_DECREF(attach_seq);
    }

    return dict;
}

PyObject *pyttd_set_ignore_patterns(PyObject *self, PyObject *args) {
    (void)self;

    PyObject *patterns_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &patterns_list)) {
        return NULL;
    }

    clear_filters();

    Py_ssize_t n = PyList_GET_SIZE(patterns_list);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = PyList_GET_ITEM(patterns_list, i);
        const char *pattern = PyUnicode_AsUTF8(item);
        if (!pattern) {
            PyErr_Clear();
            continue;
        }

        if (strchr(pattern, '/') != NULL || strchr(pattern, '\\') != NULL) {
            if (g_dir_filter.count < MAX_IGNORE_PATTERNS) {
                char *dup = strdup(pattern);
                if (dup) g_dir_filter.patterns[g_dir_filter.count++] = dup;
            }
        } else {
            if (g_exact_filter.count < MAX_IGNORE_PATTERNS * 2) {
                char *dup = strdup(pattern);
                if (dup) g_exact_filter.entries[g_exact_filter.count++] = dup;
            }
        }
    }

    Py_RETURN_NONE;
}

/* Phase 8B: Set secret patterns for variable redaction */
PyObject *pyttd_set_secret_patterns(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *patterns_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &patterns_list)) {
        return NULL;
    }
    clear_secret_filter();
    Py_ssize_t n = PyList_GET_SIZE(patterns_list);
    for (Py_ssize_t i = 0; i < n && g_secret_filter.count < MAX_IGNORE_PATTERNS; i++) {
        PyObject *item = PyList_GET_ITEM(patterns_list, i);
        const char *pattern = PyUnicode_AsUTF8(item);
        if (!pattern) { PyErr_Clear(); continue; }
        char *dup = strdup(pattern);
        if (dup) g_secret_filter.patterns[g_secret_filter.count++] = dup;
    }
    /* Item #8: rebuild the trie once after all patterns are loaded. */
    rebuild_secret_trie();
    Py_RETURN_NONE;
}

/* Phase 9B: Set include patterns for selective recording */
PyObject *pyttd_set_include_patterns(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *patterns_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &patterns_list)) {
        return NULL;
    }
    clear_include_filter();
    Py_ssize_t n = PyList_GET_SIZE(patterns_list);
    if (n > 0) {
        g_include_mode = 1;
        for (Py_ssize_t i = 0; i < n && g_include_filter.count < MAX_IGNORE_PATTERNS; i++) {
            PyObject *item = PyList_GET_ITEM(patterns_list, i);
            const char *pattern = PyUnicode_AsUTF8(item);
            if (!pattern) { PyErr_Clear(); continue; }
#ifndef _WIN32
            if (!has_glob_chars(pattern)) {
                /* Auto-wrap plain substring patterns as *pattern* for fnmatch */
                size_t len = strlen(pattern);
                char *wrapped = (char *)malloc(len + 3);
                if (wrapped) {
                    snprintf(wrapped, len + 3, "*%s*", pattern);
                    g_include_filter.patterns[g_include_filter.count++] = wrapped;
                }
            } else {
                char *dup = strdup(pattern);
                if (dup) g_include_filter.patterns[g_include_filter.count++] = dup;
            }
#else
            char *dup = strdup(pattern);
            if (dup) g_include_filter.patterns[g_include_filter.count++] = dup;
#endif
        }
    }
    Py_RETURN_NONE;
}

PyObject *pyttd_set_max_frames(PyObject *self, PyObject *args) {
    (void)self;
    unsigned long long max_frames;
    if (!PyArg_ParseTuple(args, "K", &max_frames)) {
        return NULL;
    }
    g_max_frames = (uint64_t)max_frames;
    Py_RETURN_NONE;
}

PyObject *pyttd_set_file_include_patterns(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *patterns_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &patterns_list)) return NULL;
    /* Clear existing file include patterns */
    for (int i = 0; i < g_file_include_filter.count; i++) {
        free(g_file_include_filter.patterns[i]);
        g_file_include_filter.patterns[i] = NULL;
    }
    g_file_include_filter.count = 0;
    g_file_include_mode = 0;
    Py_ssize_t n = PyList_GET_SIZE(patterns_list);
    if (n > 0) {
        g_file_include_mode = 1;
        for (Py_ssize_t i = 0; i < n && g_file_include_filter.count < MAX_IGNORE_PATTERNS; i++) {
            PyObject *item = PyList_GET_ITEM(patterns_list, i);
            const char *pattern = PyUnicode_AsUTF8(item);
            if (!pattern) { PyErr_Clear(); continue; }
            char *dup = strdup(pattern);
            if (dup) g_file_include_filter.patterns[g_file_include_filter.count++] = dup;
        }
    }
    Py_RETURN_NONE;
}

PyObject *pyttd_set_exclude_patterns(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *func_list, *file_list;
    if (!PyArg_ParseTuple(args, "O!O!", &PyList_Type, &func_list, &PyList_Type, &file_list))
        return NULL;
    /* Clear existing exclude patterns */
    for (int i = 0; i < g_exclude_func_filter.count; i++) {
        free(g_exclude_func_filter.patterns[i]);
    }
    g_exclude_func_filter.count = 0;
    for (int i = 0; i < g_exclude_file_filter.count; i++) {
        free(g_exclude_file_filter.patterns[i]);
    }
    g_exclude_file_filter.count = 0;
    g_exclude_mode = 0;

    Py_ssize_t nf = PyList_GET_SIZE(func_list);
    for (Py_ssize_t i = 0; i < nf && g_exclude_func_filter.count < MAX_IGNORE_PATTERNS; i++) {
        const char *p = PyUnicode_AsUTF8(PyList_GET_ITEM(func_list, i));
        if (!p) { PyErr_Clear(); continue; }
#ifndef _WIN32
        if (!has_glob_chars(p)) {
            size_t len = strlen(p);
            char *wrapped = (char *)malloc(len + 3);
            if (wrapped) {
                snprintf(wrapped, len + 3, "*%s*", p);
                g_exclude_func_filter.patterns[g_exclude_func_filter.count++] = wrapped;
            }
        } else {
            char *dup = strdup(p);
            if (dup) g_exclude_func_filter.patterns[g_exclude_func_filter.count++] = dup;
        }
#else
        char *dup = strdup(p);
        if (dup) g_exclude_func_filter.patterns[g_exclude_func_filter.count++] = dup;
#endif
    }
    Py_ssize_t nfi = PyList_GET_SIZE(file_list);
    for (Py_ssize_t i = 0; i < nfi && g_exclude_file_filter.count < MAX_IGNORE_PATTERNS; i++) {
        const char *p = PyUnicode_AsUTF8(PyList_GET_ITEM(file_list, i));
        if (!p) { PyErr_Clear(); continue; }
        char *dup = strdup(p);
        if (dup) g_exclude_file_filter.patterns[g_exclude_file_filter.count++] = dup;
    }
    if (g_exclude_func_filter.count > 0 || g_exclude_file_filter.count > 0) {
        g_exclude_mode = 1;
    }
    Py_RETURN_NONE;
}

/* Item #5: Set file globs whose frames record events but skip locals. */
PyObject *pyttd_set_exclude_locals_patterns(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *patterns_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &patterns_list)) return NULL;
    for (int i = 0; i < g_exclude_locals_filter.count; i++) {
        free(g_exclude_locals_filter.patterns[i]);
        g_exclude_locals_filter.patterns[i] = NULL;
    }
    g_exclude_locals_filter.count = 0;
    g_exclude_locals_mode = 0;
    Py_ssize_t n = PyList_GET_SIZE(patterns_list);
    if (n > 0) {
        g_exclude_locals_mode = 1;
        for (Py_ssize_t i = 0; i < n && g_exclude_locals_filter.count < MAX_IGNORE_PATTERNS; i++) {
            PyObject *item = PyList_GET_ITEM(patterns_list, i);
            const char *pattern = PyUnicode_AsUTF8(item);
            if (!pattern) { PyErr_Clear(); continue; }
            char *dup = strdup(pattern);
            if (dup) g_exclude_locals_filter.patterns[g_exclude_locals_filter.count++] = dup;
        }
    }
    /* Invalidate the frame-locals-exempt cache so the next LINE event re-evaluates. */
    g_cached_frame = NULL;
    Py_RETURN_NONE;
}

PyObject *pyttd_set_locals_max_depth(PyObject *self, PyObject *args) {
    (void)self;
    int depth;
    if (!PyArg_ParseTuple(args, "i", &depth)) return NULL;
    g_locals_max_depth = depth;
    g_cached_frame = NULL;  /* force re-evaluation on next LINE */
    Py_RETURN_NONE;
}

PyObject *pyttd_request_stop(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
    Py_RETURN_NONE;
}

PyObject *pyttd_set_recording_thread(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    unsigned long old_main = g_main_thread_id;
    unsigned long new_main = PyThread_get_thread_ident();
    g_main_thread_id = new_main;
    /* If the recording thread is different from the original main thread
     * (e.g., server mode: start_recording called from RPC thread, then
     * set_recording_thread called from the spawned recording thread), mark
     * the old main thread's pre-allocated ring buffer as orphaned so the
     * checkpoint skip guard (ringbuf_thread_count() <= 1) isn't tripped. */
    if (old_main != 0 && old_main != new_main) {
        ringbuf_orphan_thread(old_main);
    }
    /* Item #6: seed this thread's checkpoint deadline — start_recording
     * seeded it for the calling thread, but in server mode the recording
     * thread is different. */
    if (g_checkpoint_interval > 0) {
        g_my_next_checkpoint_seq = atomic_load_explicit(&g_sequence_counter,
                                                        memory_order_relaxed)
                                   + (uint64_t)g_checkpoint_interval;
    } else {
        g_my_next_checkpoint_seq = UINT64_MAX;
    }
    Py_RETURN_NONE;
}

PyObject *pyttd_trace_current_frame(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    /* Install the trace function on the current thread so that line events
     * fire in the caller's already-entered frame (used by arm() / attach mode).
     * Uses PyEval_SetTrace which activates CPython's internal monitoring on
     * 3.12+. The g_trace_installed_externally flag tells stop_recording to
     * perform extra cleanup to fully reset the monitoring state. */
    if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) {
        Py_RETURN_NONE;
    }
    PyThreadState *tstate = PyThreadState_Get();
    if (tstate->c_tracefunc != (Py_tracefunc)pyttd_trace_func) {
        PyEval_SetTrace((Py_tracefunc)pyttd_trace_func, Py_None);
        g_trace_installed_externally = 1;
    }
    Py_RETURN_NONE;
}

/* ---- User Pause API (live debugging) ---- */

PyObject *pyttd_request_user_pause(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) {
        return PyBool_FromLong(0);
    }
    if (atomic_load_explicit(&g_user_paused, memory_order_relaxed)) {
        /* Already paused */
        return PyBool_FromLong(1);
    }

    /* Set the flag. All recording threads will check it and park.
     * We signal as soon as the first thread parks (see trace function handler). */
    atomic_store_explicit(&g_user_pause_thread_count, 0, memory_order_release);
    atomic_store_explicit(&g_user_pause_requested, 1, memory_order_release);

    /* Wait for ack with 5-second timeout.
     * CRITICAL: Release the GIL during the wait — the recording thread needs
     * the GIL to execute user code and fire LINE events where the pause check
     * happens.  Without releasing the GIL here, the recording thread would be
     * blocked waiting for the GIL forever. */
    int success = 0;
    Py_BEGIN_ALLOW_THREADS
#ifdef _WIN32
    EnterCriticalSection(&g_user_pause_cs);
    DWORD deadline = GetTickCount() + 10000;
    while (!atomic_load_explicit(&g_user_paused, memory_order_acquire)) {
        DWORD remaining = deadline - GetTickCount();
        if (remaining > 10000) break;  /* wrapped */
        if (!SleepConditionVariableCS(&g_user_pause_cv, &g_user_pause_cs, remaining)) {
            break;  /* timeout */
        }
    }
    success = atomic_load_explicit(&g_user_paused, memory_order_acquire);
    LeaveCriticalSection(&g_user_pause_cs);
#else
    struct timespec abs_timeout;
    clock_gettime(CLOCK_REALTIME, &abs_timeout);
    abs_timeout.tv_sec += 10;  /* 10s timeout for CPU-bound scripts */

    pthread_mutex_lock(&g_user_pause_mutex);
    while (!atomic_load_explicit(&g_user_paused, memory_order_acquire)) {
        int rc = pthread_cond_timedwait(&g_user_pause_ack_cv, &g_user_pause_mutex, &abs_timeout);
        if (rc != 0) break;  /* timeout or error */
    }
    success = atomic_load_explicit(&g_user_paused, memory_order_acquire);
    pthread_mutex_unlock(&g_user_pause_mutex);
#endif
    Py_END_ALLOW_THREADS

    if (!success) {
        /* Timeout — clear request */
        atomic_store_explicit(&g_user_pause_requested, 0, memory_order_relaxed);
    }

    return PyBool_FromLong(success);
}

PyObject *pyttd_user_resume(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    if (!atomic_load_explicit(&g_user_paused, memory_order_relaxed)) {
        Py_RETURN_NONE;
    }

    /* Clear paused state and broadcast to wake ALL parked threads */
    atomic_store_explicit(&g_user_pause_requested, 0, memory_order_release);
    atomic_store_explicit(&g_user_paused, 0, memory_order_release);
#ifdef _WIN32
    EnterCriticalSection(&g_user_pause_cs);
    WakeAllConditionVariable(&g_user_pause_cv);
    LeaveCriticalSection(&g_user_pause_cs);
#else
    pthread_mutex_lock(&g_user_pause_mutex);
    pthread_cond_broadcast(&g_user_pause_cv);
    pthread_mutex_unlock(&g_user_pause_mutex);
#endif
    Py_RETURN_NONE;
}

PyObject *pyttd_is_user_paused(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    return PyBool_FromLong(atomic_load_explicit(&g_user_paused, memory_order_relaxed));
}

PyObject *pyttd_get_sequence_counter(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    uint64_t seq = atomic_load_explicit(&g_sequence_counter, memory_order_relaxed);
    return PyLong_FromUnsignedLongLong(seq);
}

PyObject *pyttd_flush_and_wait(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    /* Signal the flush thread to do an immediate cycle and wait for completion.
     * Only works on Unix (flush thread uses pthreads condvar). On Windows,
     * we just sleep for one flush interval as a simple fallback. */
#ifdef _WIN32
    Sleep(g_flush_interval_ms + 5);
#else
    if (!g_flush_thread_created) {
        Py_RETURN_NONE;
    }
    pthread_mutex_lock(&g_flush_mutex);
    atomic_store_explicit(&g_flush_immediate, 1, memory_order_release);
    pthread_cond_signal(&g_flush_cond);  /* wake flush thread immediately */
    /* Wait for flush thread to signal completion */
    struct timespec abs_timeout;
    clock_gettime(CLOCK_REALTIME, &abs_timeout);
    abs_timeout.tv_sec += 2;
    pthread_cond_timedwait(&g_flush_done_cv, &g_flush_mutex, &abs_timeout);
    pthread_mutex_unlock(&g_flush_mutex);
#endif
    Py_RETURN_NONE;
}

PyObject *pyttd_set_socket_fd(PyObject *self, PyObject *args) {
    (void)self;
    int fd;
    if (!PyArg_ParseTuple(args, "i", &fd)) return NULL;
    g_server_socket_fd = fd;
    Py_RETURN_NONE;
}

PyObject *pyttd_set_variable(PyObject *self, PyObject *args) {
    (void)self;
    const char *var_name;
    const char *new_value_expr;
    if (!PyArg_ParseTuple(args, "ss", &var_name, &new_value_expr)) return NULL;

    /* Only works when user-paused — live frame must be accessible */
    if (!atomic_load_explicit(&g_user_paused, memory_order_relaxed)) {
        PyErr_SetString(PyExc_RuntimeError, "set_variable: not paused");
        return NULL;
    }

    PyObject *frame = g_main_paused_frame;
    if (!frame) {
        PyErr_SetString(PyExc_RuntimeError, "set_variable: no paused frame available");
        return NULL;
    }

    /* Evaluate the new value expression with restricted builtins.
     * Import builtins and build a restricted globals dict. */
    PyObject *builtins_mod = PyImport_ImportModule("builtins");
    if (!builtins_mod) return NULL;

    PyObject *safe_globals = PyDict_New();
    if (!safe_globals) { Py_DECREF(builtins_mod); return NULL; }

    /* Add safe builtins: len, str, int, float, bool, list, dict, tuple, set,
     * type, bytes, True, False, None, abs, min, max, sum, round, repr, range */
    const char *safe_names[] = {
        "len", "str", "int", "float", "bool", "list", "dict", "tuple", "set",
        "type", "bytes", "bytearray", "frozenset", "complex", "range", "slice",
        "isinstance", "issubclass", "abs", "min", "max", "sum", "round", "pow",
        "all", "any", "enumerate", "zip", "sorted", "reversed",
        "repr", "ascii", "chr", "ord", "hex", "oct", "bin",
        "True", "False", "None", "hash", "id", "callable",
        NULL
    };
    for (int i = 0; safe_names[i]; i++) {
        PyObject *obj = PyObject_GetAttrString(builtins_mod, safe_names[i]);
        if (obj) {
            PyDict_SetItemString(safe_globals, safe_names[i], obj);
            Py_DECREF(obj);
        } else {
            PyErr_Clear();
        }
    }
    /* Set __builtins__ to the restricted dict to prevent import/exec/eval */
    PyDict_SetItemString(safe_globals, "__builtins__", safe_globals);
    Py_DECREF(builtins_mod);

    /* Evaluate the expression */
    PyObject *new_value = PyRun_String(new_value_expr, Py_eval_input, safe_globals, safe_globals);
    Py_DECREF(safe_globals);
    if (!new_value) {
        /* Evaluation failed — return the error to the caller */
        return NULL;
    }

    /* Get the frame's locals and set the variable */
    PyObject *locals = PyFrame_GetLocals((PyFrameObject *)frame);
    if (!locals) {
        Py_DECREF(new_value);
        PyErr_SetString(PyExc_RuntimeError, "set_variable: failed to get frame locals");
        return NULL;
    }

    PyObject *name_obj = PyUnicode_FromString(var_name);
    if (!name_obj) {
        Py_DECREF(locals);
        Py_DECREF(new_value);
        return NULL;
    }

    /* Read old value for the result */
    PyObject *old_value = PyObject_GetItem(locals, name_obj);
    if (!old_value) {
        PyErr_Clear();  /* Variable may not exist yet — that's OK for new vars */
    }

    /* Version-gated set:
     * Python 3.13+ (PEP 667): PyFrame_GetLocals returns FrameLocalsProxy
     *   which supports __setitem__ that writes through to fast locals.
     * Python 3.12: PyFrame_GetLocals returns a dict snapshot.
     *   PyDict_SetItem modifies the snapshot but doesn't sync to fast locals.
     *   We use PyObject_SetItem which works for both dict and FrameLocalsProxy. */
    int rc = PyObject_SetItem(locals, name_obj, new_value);

    Py_DECREF(locals);
    Py_DECREF(name_obj);

    if (rc < 0) {
        Py_DECREF(new_value);
        Py_XDECREF(old_value);
        return NULL;
    }

    /* Build repr BEFORE releasing new_value reference */
    PyObject *new_repr = PyObject_Repr(new_value);
    Py_DECREF(new_value);

    /* Return result dict with old and new repr */
    PyObject *result = PyDict_New();
    if (result) {
        PyObject *name_str = PyUnicode_FromString(var_name);
        PyObject *old_repr = old_value ? PyObject_Repr(old_value) : PyUnicode_FromString("<undefined>");

        if (name_str) { PyDict_SetItemString(result, "name", name_str); Py_DECREF(name_str); }
        if (new_repr) { PyDict_SetItemString(result, "value", new_repr); Py_DECREF(new_repr); }
        if (old_repr) { PyDict_SetItemString(result, "oldValue", old_repr); Py_DECREF(old_repr); }
    }
    Py_XDECREF(old_value);

    return result;
}
