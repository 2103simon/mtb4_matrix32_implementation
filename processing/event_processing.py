from collections import deque


class EventProcessor:

    def __init__(self, max_history=1000):

        self.history = deque(maxlen=max_history)

    def process(self, events):
        """raw_event_time_ms, corrected_event_time_ms, id0, id1"""
        # Store received event batch
        self.history.append(list(events))

        print(f"[EVENTS] Received {len(events)} events")

        for event in events:

            _, corrected_timestamp , id0, id1 = event

            print(
                f"Timestamp: {corrected_timestamp}, "
                f"ID0: {id0}, ID1: {id1}"
            )

        return events