"""A fake driver for --mock: serves the tags listed in the PLC's ``mock_tags`` JSON file."""

import json
import random

from ciplink import Tag

INTS = {"SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT"}
FLOATS = {"REAL", "LREAL"}


def initial(data_type):
    if data_type == "BOOL":
        return False
    if data_type in INTS:
        return 0
    if data_type in FLOATS:
        return 0.0
    if data_type == "STRING":
        return ""
    return None


class MockDriver:
    """Same calls as LogixDriver as far as the worker uses them; numbers random-walk, BOOLs toggle."""

    def __init__(self, cfg):
        types = {}
        if cfg.mock_tags:
            with open(cfg.mock_tags, encoding="utf-8") as file:
                types = json.load(file)
        self.types = types
        self.values = {name: initial(data_type) for name, data_type in types.items()}
        self.data_types = {}
        self._tag_table_version = 1
        self._random = random.Random()

    @property
    def tags(self):
        return {name: {"tag_name": name, "tag_type": "atomic", "data_type": t, "data_type_name": t}
                for name, t in self.types.items()}

    def open(self):
        return True

    def close(self):
        pass

    def _step(self, name):
        data_type, value = self.types[name], self.values[name]
        if data_type == "BOOL":
            if self._random.random() < 0.1:
                value = not value
        elif data_type in INTS:
            value += self._random.choice((-1, 0, 1))
        elif data_type in FLOATS:
            value = round(value + self._random.uniform(-1, 1), 3)
        self.values[name] = value
        return value

    def read(self, *tags):
        results = []
        for name in tags:
            if name not in self.types:
                results.append(Tag(name, None, None, f"Tag doesn't exist - {name}"))
            elif self.values[name] is None:
                results.append(Tag(name, None, self.types[name], f"mock does not simulate {self.types[name]}"))
            else:
                results.append(Tag(name, self._step(name), self.types[name], None))
        return results[0] if len(results) == 1 else results

    def write(self, name, value):
        if name not in self.types:
            return Tag(name, None, None, f"Tag doesn't exist - {name}")
        self.values[name] = value
        return Tag(name, value, self.types[name], None)
