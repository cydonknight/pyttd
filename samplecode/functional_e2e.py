"""End-to-end functional test script for pyttd.

Exercises: function calls, recursion, exceptions, loops, nested calls,
time/random I/O hooks, local variable mutations.
"""
import time
import random


def fibonacci(n):
    """Recursive fibonacci — exercises deep call stacks."""
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)


def divide(a, b):
    """May raise ZeroDivisionError."""
    return a / b


def process_items(items):
    """Loop with local mutations and nested calls."""
    results = []
    for i, item in enumerate(items):
        transformed = item.upper()
        length = len(transformed)
        results.append((transformed, length))
    return results


def risky_operations():
    """Exercises exception handling paths."""
    results = []
    for val in [10, 5, 0, 3]:
        try:
            result = divide(100, val)
            results.append(result)
        except ZeroDivisionError as e:
            results.append(f"error: {e}")
    return results


def timed_work():
    """Exercises I/O hooks (time.time, random)."""
    start = time.time()
    mono = time.monotonic()
    perf = time.perf_counter()
    r = random.random()
    ri = random.randint(1, 100)
    return {
        'time': start,
        'monotonic': mono,
        'perf_counter': perf,
        'random': r,
        'randint': ri,
    }


def nested_calls():
    """Tests call depth tracking."""
    def inner_a(x):
        return inner_b(x + 1)

    def inner_b(x):
        return x * 2

    return inner_a(5)


def main():
    print("=== pyttd functional test ===")

    # Simple calls
    fib_result = fibonacci(6)
    print(f"fibonacci(6) = {fib_result}")

    # String processing
    items = ["hello", "world", "pyttd", "test"]
    processed = process_items(items)
    print(f"processed = {processed}")

    # Exception handling
    risky = risky_operations()
    print(f"risky = {risky}")

    # I/O hooks
    timing = timed_work()
    print(f"timing = {timing}")

    # Nested calls
    nested = nested_calls()
    print(f"nested = {nested}")

    # Local variable with special chars (tests JSON escaping)
    special = 'hello "world"\nwith\ttabs\\and\\backslashes'
    quote_test = "it's a 'test'"

    print("=== done ===")


if __name__ == "__main__":
    main()
