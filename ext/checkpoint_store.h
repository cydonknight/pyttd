#ifndef PYTTD_CHECKPOINT_STORE_H
#define PYTTD_CHECKPOINT_STORE_H
#include <Python.h>
#include <stdint.h>

#define MAX_CHECKPOINTS 32

typedef struct {
    int child_pid;
    int cmd_fd;
    int result_fd;
    uint64_t sequence_no;       /* original checkpoint position (immutable) */
    uint64_t current_position;  /* updated after each RESUME/STEP */
    int is_alive;
    int is_busy;                /* 1 during active RESUME/STEP I/O */
} CheckpointEntry;

/* Initialize/reset the checkpoint store */
void checkpoint_store_init(void);

/* Add a checkpoint. Handles eviction if full. Returns index (0..MAX-1) or -1. */
int checkpoint_store_add(int child_pid, int cmd_fd, int result_fd, uint64_t sequence_no);

/* Find entry with largest current_position <= target_seq. Returns index or -1. */
int checkpoint_store_find_nearest(uint64_t target_seq);

/* Lookup by pid. Returns index or -1. */
int checkpoint_store_find_by_pid(int child_pid);

/* Update the current_position of entry at index. */
void checkpoint_store_update_position(int index, uint64_t new_position);

/* Send DIE, close fds, waitpid, mark dead. */
void checkpoint_store_evict(int index);

/* Thinning algorithm. Returns index of entry to evict, or -1. */
int checkpoint_to_evict(void);

/* Get entry at index (may be dead). */
CheckpointEntry *checkpoint_store_get(int index);

/* Count of live entries. */
int checkpoint_store_count(void);

/* Populate out_fds with cmd_fd/result_fd from all live entries.
 * Returns count. out_fds must have space for MAX_CHECKPOINTS * 2. */
int checkpoint_store_get_all_fds(int *out_fds);

/* Python-facing: send DIE to all checkpoint children */
PyObject *pyttd_kill_all_checkpoints(PyObject *self, PyObject *Py_UNUSED(args));

/* Python-facing: return count of live checkpoints */
PyObject *pyttd_get_checkpoint_count(PyObject *self, PyObject *Py_UNUSED(args));

#endif
