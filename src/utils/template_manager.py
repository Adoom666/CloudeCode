"""Template manager for copying template files to new sessions."""

import shutil
from pathlib import Path
from typing import Optional, Set
import structlog

logger = structlog.get_logger()


# Files and directories to exclude from template copying
EXCLUDE_PATTERNS: Set[str] = {
    ".git",
    ".gitignore",
    "__pycache__",
    "*.pyc",
    ".DS_Store",
    "node_modules",
    ".venv",
    "venv",
    ".env",
    ".env.local",
    "*.log",
    ".pytest_cache",
    ".mypy_cache",
    ".coverage",
    "dist",
    "build",
    "*.egg-info"
}


def should_exclude(path: Path) -> bool:
    """
    Check if a path should be excluded from copying.

    Args:
        path: Path to check

    Returns:
        True if path should be excluded
    """
    name = path.name

    # Check exact matches
    if name in EXCLUDE_PATTERNS:
        return True

    # Check wildcard patterns
    for pattern in EXCLUDE_PATTERNS:
        if "*" in pattern:
            # Simple wildcard matching
            if pattern.startswith("*"):
                suffix = pattern[1:]
                if name.endswith(suffix):
                    return True
            elif pattern.endswith("*"):
                prefix = pattern[:-1]
                if name.startswith(prefix):
                    return True

    return False


def copy_templates(template_path: str, destination_path: str) -> tuple[bool, Optional[str]]:
    """
    Copy template files to destination directory.

    Args:
        template_path: Source template directory path
        destination_path: Destination directory path

    Returns:
        Tuple of (success: bool, error_message: Optional[str])
    """
    try:
        template_dir = Path(template_path).expanduser().resolve()
        dest_dir = Path(destination_path).expanduser().resolve()

        # Validate template directory exists
        if not template_dir.exists():
            error_msg = f"Template directory not found: {template_dir}"
            logger.warning("template_directory_missing", path=str(template_dir))
            return False, error_msg

        if not template_dir.is_dir():
            error_msg = f"Template path is not a directory: {template_dir}"
            logger.error("template_path_not_directory", path=str(template_dir))
            return False, error_msg

        # Ensure destination exists
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Copy files recursively, excluding patterns
        copied_count = 0
        skipped_count = 0

        for item in template_dir.rglob("*"):
            # Skip if any part of path should be excluded
            if any(should_exclude(part) for part in item.relative_to(template_dir).parents) or should_exclude(item):
                skipped_count += 1
                continue

            # Calculate relative path
            rel_path = item.relative_to(template_dir)
            dest_item = dest_dir / rel_path

            try:
                if item.is_dir():
                    dest_item.mkdir(parents=True, exist_ok=True)
                    logger.debug("template_dir_created", path=str(rel_path))
                elif item.is_file():
                    dest_item.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest_item)
                    copied_count += 1
                    logger.debug("template_file_copied", path=str(rel_path))
            except Exception as e:
                logger.warning(
                    "template_item_copy_failed",
                    path=str(rel_path),
                    error=str(e)
                )
                skipped_count += 1

        logger.info(
            "templates_copied",
            template_dir=str(template_dir),
            dest_dir=str(dest_dir),
            copied=copied_count,
            skipped=skipped_count
        )

        return True, None

    except Exception as e:
        error_msg = f"Template copy failed: {str(e)}"
        logger.error(
            "template_copy_error",
            template_path=template_path,
            destination_path=destination_path,
            error=str(e)
        )
        return False, error_msg


def validate_template_path(template_path: Optional[str]) -> tuple[bool, Optional[str]]:
    """
    Validate that a template path exists and is accessible.

    Args:
        template_path: Path to validate

    Returns:
        Tuple of (valid: bool, error_message: Optional[str])
    """
    if not template_path:
        return False, "No template path specified"

    try:
        path = Path(template_path).expanduser().resolve()

        if not path.exists():
            return False, f"Template path does not exist: {path}"

        if not path.is_dir():
            return False, f"Template path is not a directory: {path}"

        # Check if readable
        if not path.stat().st_mode & 0o444:  # Check read permission
            return False, f"Template path is not readable: {path}"

        return True, None

    except Exception as e:
        return False, f"Template path validation error: {str(e)}"
