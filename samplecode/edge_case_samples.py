"""Edge case samples for testing the recorder."""

# Generator function
def fibonacci(n):
    a, b = 0, 1
    for _ in range(n):
        yield a
        a, b = b, a + b

list(fibonacci(10))


# Nested exception handling
def risky_divide(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        return float('inf')

risky_divide(10, 0)
risky_divide(10, 2)


# Recursive function
def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)

factorial(5)


# Deeply nested calls
def level3():
    return 42

def level2():
    return level3()

def level1():
    return level2()

def level0():
    return level1()

level0()


# Exception propagation
def inner_raise():
    raise RuntimeError("inner error")

def middle():
    return inner_raise()

def outer():
    try:
        middle()
    except RuntimeError:
        pass

outer()


# Multiple return paths
def classify(x):
    if x > 0:
        return "positive"
    elif x < 0:
        return "negative"
    else:
        return "zero"

classify(5)
classify(-3)
classify(0)
