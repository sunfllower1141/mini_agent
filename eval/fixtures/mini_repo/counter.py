"""Counter module with an intentional off-by-one bug.

The function count_to(n) should return [1, 2, ..., n] but has a bug.
"""


def count_to(n: int) -> list[int]:
    """Return a list of integers from 1 to n (inclusive)."""
    return list(range(1, n))  # BUG: should be range(1, n + 1)


if __name__ == "__main__":
    print(count_to(5))
