"""Safe provider error codes; never retain arbitrary exception text."""


class DecisionError(RuntimeError):
    pass
