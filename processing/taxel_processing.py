from collections import deque


class TaxelProcessor:

    def __init__(self, max_history=1000):

        # Store the last 1000 samples
        self.history = deque(maxlen=max_history)

        self.latest_values = None
        self.latest_corrected_timestamp = None
        self.latest_raw_timestamp = None

    def process(self, values, corrected_timestamp, raw_timestamp):
        """taxel_values, corrected_timestamp_ms, raw_timestamp_ms"""
        
        # Store latest measurement
        self.latest_values = list(values)
        self.latest_corrected_timestamp = corrected_timestamp
        self.latest_raw_timestamp = raw_timestamp

        # Create sample
        sample = {
            "corrected_timestamp": corrected_timestamp,
            "raw_timestamp": raw_timestamp,
            "values": list(values)
        }

        # Save sample
        self.history.append(sample)

        # Print sample
        print(f"[TAXEL] {corrected_timestamp}: {values}")

        return sample
    
    