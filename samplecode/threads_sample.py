"""Threading sample — threads are ignored in Phase 1 (only main thread recorded)."""
import threading

results = []

def worker(n):
    total = sum(range(n))
    results.append(total)

threads = []
for i in range(3):
    t = threading.Thread(target=worker, args=(1000 * (i + 1),))
    threads.append(t)
    t.start()

for t in threads:
    t.join()

print(f"Thread results: {results}")
