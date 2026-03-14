class LowPassFilter:
    def __init__(self, alpha):
        self.alpha = alpha
        self.smoothed_value = None

    def update(self, raw_value):
        if self.smoothed_value is None:
            self.smoothed_value = raw_value
        else:
            self.smoothed_value = (self.alpha * raw_value) + ((1 - self.alpha) * self.smoothed_value)
        return self.smoothed_value