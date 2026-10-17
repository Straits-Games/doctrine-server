"""The single error type the logic modules raise; api.py turns it into {"detail": code}."""


class ApiError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code
