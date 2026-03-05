from sys import _getframe
from types import FrameType
from typing import List


def testfuncA(input):
    a = input
    b = 5
    c = a + b
    a = 6
    try:
        raise Exception("Test Exception")
    except Exception as e:
        pass
    return c - 1

testfuncA(1)

def binarysearch(nums, target):
    left = 0
    right = len(nums) -1

    while left<= right:
        mid = (left+right)//2

        if nums[mid] == target:
            return nums[mid]
        elif nums[mid] > target:
            right = mid - 1
        else:
            left = mid + 1

    return -1


def traceback_frames(frame: FrameType = None):
    if not frame:
        frame = _getframe(1)
    number_of_frames = 0
    while frame:
        print("Line number - ", frame.f_lineno)
        print("Function name - ", frame.f_code.co_name)
        print("Filename - ", frame.f_code.co_filename)
        print("First line number - ", frame.f_code.co_firstlineno)
        if frame.f_code.co_name == "<module>":
            print("Module level")
            print("Module name ", __name__)
        print("----------------------")
        frame = frame.f_back
        number_of_frames += 1

    print("Number of frames traced: ", number_of_frames)

def testing():
    a = 1
    traceback_frames()

def supertesting():
    b = 2
    testing()

def superdupertesting(arg):
    c = 5
    test = arg - c
    supertesting()

superdupertesting(4)

def binsearch2(nums, target):
    left = 0
    right = len(nums) - 1

    while left < right:
        mid = (left + right) // 2
        if nums[mid] == target:
            right = mid
        else:
            left = mid + 1

    return left

class TreeNode:
    def __init__(self, val: int, left=None, right=None) -> None:
        self.val = val
        self.left = left
        self.right = right

    def __repr__(self) -> str:
        return f"val: {self.val}, left: {self.left}, right: {self.right}"

    def __str__(self) -> str:
        return str(self.val)

def to_binary_tree2(items: List[int]) -> TreeNode:
    arrlength = len(items)
    if not arrlength:
        return None

    def inner(index: int = 0) -> TreeNode:
        if index >= arrlength or not items[index]:
            return None

        node = TreeNode(items[index])
        node.left = inner(2*index + 1)
        node.right = inner(2*index +2)
        return node

    return inner()

def to_binary_tree(items: list[int]) -> TreeNode:
    length = len(items)
    if length == 0:
        return None

    def inner(index: int = 0) -> TreeNode:
        if index >= length or not items[index]:
            return None

        node = TreeNode(items[index])
        node.left = inner(2*index+1)
        node.right = inner(2*index+2)
        return node

    return inner()

def n_frequent_words(posting: str="", n: int=1) -> List[int]:
    wordmap = {}
    posting_list = posting.split()

    for word in posting_list:
        clean_word = word.strip(".,").lower()
        wordmap[clean_word] = wordmap.get(clean_word, 0) + 1

    sorted_frequency = sorted(wordmap.items(), key=lambda x: x[1], reverse=True)

    print(sorted_frequency[:n])
