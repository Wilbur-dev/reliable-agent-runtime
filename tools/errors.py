from __future__ import annotations


class ToolError(Exception):
    error_type = "tool_error"


class PathBoundaryError(ToolError):
    error_type = "path_boundary_error"


class FileTooLargeError(ToolError):
    error_type = "file_too_large"


class FileEncodingError(ToolError):
    error_type = "file_encoding_error"
