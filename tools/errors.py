from __future__ import annotations


class ToolError(Exception):
    error_type = "tool_error"


class PathBoundaryError(ToolError):
    error_type = "path_boundary_error"


class FileTooLargeError(ToolError):
    error_type = "file_too_large"


class FileEncodingError(ToolError):
    error_type = "file_encoding_error"


class ProtectedPathError(ToolError):
    error_type = "protected_path"


class PatchApplyError(ToolError):
    error_type = "patch_apply_error"


class CommandNotAllowedError(ToolError):
    error_type = "command_not_allowed"


class CommandTimeoutError(ToolError):
    error_type = "command_timeout"
