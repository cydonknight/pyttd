"""Edge cases for functional testing."""

# 1. Unicode in variables
emoji_var = "Hello 🌍"
japanese = "こんにちは"
backslash_str = 'path\\to\\file "quoted"'

# 2. Large locals
big_list = list(range(500))
big_dict = {f"key_{i}": i * i for i in range(200)}

# 3. Nested data structures
nested = {"a": [1, {"b": [2, 3, {"c": 4}]}]}

# 4. None, bool, special values
nothing = None
flag = True
inf_val = float('inf')
nan_val = float('nan')

# 5. Generator (not serializable)
def gen():
    yield 1
    yield 2
g = gen()

# 6. Class instances
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y
    def __repr__(self):
        return f"Point({self.x}, {self.y})"

p = Point(3, 4)

# 7. Deeply nested calls
def level_a():
    return level_b()

def level_b():
    return level_c()

def level_c():
    return level_d()

def level_d():
    return "deep"

deep_result = level_a()

# 8. Lambda
square = lambda x: x * x
squares = [square(i) for i in range(5)]

# 9. Multiple exceptions
errors = []
for i in range(3):
    try:
        1 / 0
    except ZeroDivisionError as e:
        errors.append(str(e))
    try:
        [][99]
    except IndexError as e:
        errors.append(str(e))

print(f"Done: {len(errors)} errors caught")
