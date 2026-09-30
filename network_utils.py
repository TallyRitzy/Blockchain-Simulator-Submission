"""Small bounds shared by the node and discovery service."""
from collections import OrderedDict


class RecentMessageIds:
    def __init__(self, limit=10000):
        self.limit = limit
        self.items = OrderedDict()

    def __contains__(self, value):
        return value in self.items

    def add(self, value):
        self.items[value] = None
        self.items.move_to_end(value)
        if len(self.items) > self.limit:
            self.items.popitem(last=False)

    def __len__(self):
        return len(self.items)
