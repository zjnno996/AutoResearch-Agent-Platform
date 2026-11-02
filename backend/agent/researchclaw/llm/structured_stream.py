"""Recognize repeated closing tokens after one complete JSON root.

This never imposes a response length limit or repairs missing syntax. Scientific
validators still decide whether the preserved complete object is acceptable.
"""
import hashlib
import json


class JSONClosingRepetition:
    def __init__(self):
        self.parts = []
        self.stack = []
        self.started = False
        self.disabled = False
        self.in_string = False
        self.escaped = False
        self.complete_text = None
        self.valid_complete_root = False
        self.trailing_closers = 0

    def feed(self, text: str):
        if self.disabled:
            return None
        for index, char in enumerate(text):
            if self.complete_text is not None:
                if char.isspace():
                    continue
                if char not in '}]':
                    # A prematurely closed root followed by more members may
                    # contain meaningful material. Never silently remove it.
                    self.disabled = True
                    return None
                self.trailing_closers += 1
                continue
            if not self.started:
                if char.isspace():
                    continue
                if char not in '{[':
                    self.disabled = True
                    return None
                self.started = True
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif char == '\\':
                    self.escaped = True
                elif char == '"':
                    self.in_string = False
                continue
            if char == '"':
                self.in_string = True
            elif char in '{[':
                self.stack.append('}' if char == '{' else ']')
            elif char in '}]':
                if not self.stack or self.stack.pop() != char:
                    self.disabled = True
                    return None
                if not self.stack:
                    self.complete_text = ''.join(self.parts)+text[:index+1]
                    try:
                        def reject_constant(value):
                            raise ValueError('Non-JSON constant')
                        value = json.loads(self.complete_text, parse_constant=reject_constant)
                        self.valid_complete_root = isinstance(value, (dict,list))
                    except ValueError:
                        self.valid_complete_root = False
        if self.complete_text is None:
            self.parts.append(text)
        elif self.trailing_closers >= 64:
            return {'kind':'json_closing_repetition',
                    'valid_complete_root':self.valid_complete_root,
                    'trailing_closers':self.trailing_closers,
                    'complete_content_sha256':hashlib.sha256(self.complete_text.encode()).hexdigest(),
                    'usage_complete':False}
        return None
