class DriveError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class NotFound(DriveError):
    def __init__(self, message: str = "that file or folder is not here any more"):
        super().__init__(message, 404)


class Conflict(DriveError):
    def __init__(self, message: str):
        super().__init__(message, 409)
