"""Basic script for functional testing."""
import time
import random

def fibonacci(n):
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)

def greet(name):
    msg = f"Hello, {name}!"
    return msg

def risky(x):
    if x == 0:
        raise ValueError("Cannot divide by zero")
    return 100 / x

# Main execution
result = fibonacci(6)
print(f"fib(6) = {result}")

names = ["Alice", "Bob", "Charlie"]
for name in names:
    print(greet(name))

# Exception handling
for x in [5, 3, 0, 2]:
    try:
        val = risky(x)
        print(f"risky({x}) = {val}")
    except ValueError as e:
        print(f"Caught: {e}")

# I/O hooks
t = time.time()
r = random.random()
print(f"time={t:.2f}, random={r:.4f}")
