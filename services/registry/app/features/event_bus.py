"""Event Bus: Real-time event broadcasting for dashboard live feed."""
from collections import defaultdict
from datetime import datetime

class EventBus:
    def __init__(self):
        self.subscribers = defaultdict(list)
    
    def publish(self, channel, event):
        for cb in self.subscribers.get(channel, []):
            cb(event)
    
    def subscribe(self, channel, callback):
        self.subscribers[channel].append(callback)

event_bus = EventBus()
