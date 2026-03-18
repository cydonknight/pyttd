"""pyttd example — record with: pyttd record examples/hello.py"""


def greet(name):
    message = f"Hello, {name}!"
    print(message)
    return message


def main():
    names = ["Alice", "Bob", "Charlie"]
    results = []
    for name in names:
        results.append(greet(name))
    print(f"Greeted {len(results)} people")


main()
