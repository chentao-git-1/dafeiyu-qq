"""Bounded diagnostic codes; never retain prompts, credentials or provider bodies."""
import re


class PostGenerationError(RuntimeError):
    def __init__(self, code):
        self.code = code if re.fullmatch(r'[A-Za-z0-9_]{1,80}', str(code)) else 'generation_failed'
        super().__init__(self.code)


def error_code(error):
    return error.code if isinstance(error, PostGenerationError) else type(error).__name__
