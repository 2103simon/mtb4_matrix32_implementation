import numpy as np
from collections import deque

class DataAnalysis:

    def __init__(self):
        history_len = 1000
        self.history = deque(maxlen=history_len)

    def analyze_taxel(self, sample):

        values = sample["values"]
        self.history.append(values)
        history = np.array(self.history)
        result = {
            "timestamp": sample["corrected_timestamp"],
            "mean": np.sum(history, axis=0) / len(history),
            "minimum": np.min(history, axis=0),
            "maximum": np.max(history, axis=0)
        }

        print("[ANALYSIS]", result)

        return result