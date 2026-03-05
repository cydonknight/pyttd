

class TracePerformance:
    def __init__(self):
        self.total_samples = 1
        self.total_time = 0.00

    def record_sample(self, execution_time: float):
        self.total_samples += 1
        self.total_time += execution_time

    def performance_stats(self):
        return {
            "total_samples": self.total_samples,
            "total_time": self.total_time,
            "average_sample_time": self.total_time / self.total_samples
        }

