class PharaohError(Exception):
    """An error with an HTTP status that is safe to show to the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
